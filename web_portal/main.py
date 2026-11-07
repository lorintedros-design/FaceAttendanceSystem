from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse
from io import BytesIO
import base64
from datetime import datetime
import qrcode
from pydantic import BaseModel, Field
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import sqlite3

from security import auth as desktop_auth
from web_portal.database import get_student, list_students
from web_portal import sessions as session_store
from web_portal.attendance import mark_web_attendance
from web_portal.face_verification import IMAGE_DIR, prepare_face_registration_photo, verify_student_liveness

# ==========================================
# ATTENDIQ - SMART QR ATTENDANCE WEB PORTAL
# ==========================================

app = FastAPI(
    title="AttendIQ",
    description="Secure QR, Face and GPS Attendance System",
    version="1.0.0"
)

_basic_auth = HTTPBasic(auto_error=False)
_login_throttle = desktop_auth.LoginThrottle()
_face_throttle = desktop_auth.LoginThrottle(max_failures=8, lockout=60)


def require_staff(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic_auth),
):
    """Authenticate an existing active Admin or Teacher account from the desktop app."""
    client_host = request.client.host if request.client else ""
    is_local = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.headers.get("origin") != expected_origin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request origin is not allowed.")

    if request.url.scheme != "https" and not is_local:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="HTTPS is required for remote portal access.",
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in with your AttendIQ staff account.",
            headers={"WWW-Authenticate": "Basic"},
        )

    username = credentials.username.strip()
    if _login_throttle.locked_for(username):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts. Wait before trying again.",
        )

    connection = sqlite3.connect(desktop_auth.DB_PATH, timeout=5)
    try:
        with connection:
            user = desktop_auth.authenticate(
                connection, username, credentials.password, record_login=False
            )
    except sqlite3.OperationalError as error:
        if "no such table: users" in str(error).lower():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Open AttendIQ on the desktop and finish staff account setup first.",
            ) from error
        raise
    finally:
        connection.close()

    if user is None:
        _login_throttle.record_failure(username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
            headers={"WWW-Authenticate": "Basic"},
        )
    _login_throttle.reset(username)
    if user["role"] not in desktop_auth.ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff access required.")
    return user


def teacher_public_base_url():
    """Return the current temporary public URL, if the desktop has a live tunnel."""
    connection = sqlite3.connect(desktop_auth.DB_PATH, timeout=3)
    try:
        row = connection.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key='teacher_public_url'"
        ).fetchone()
        return str(row[0]).rstrip("/") if row and row[0] else ""
    except sqlite3.Error:
        return ""
    finally:
        connection.close()


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>AttendIQ</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { background: #07101f; color: white; font-family: Arial, sans-serif;
                   text-align: center; padding: 60px 15px; }
            .card { background: #101d32; padding: 35px; border-radius: 16px;
                    max-width: 450px; margin: auto; }
            h1 { color: #00c8f0; }
            p { color: #b8c7dc; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>AttendIQ</h1>
            <h2>Smart Attendance System</h2>
            <p>QR Code + Face Verification + GPS Security</p>
            <p>Web backend is running successfully!</p>
        </div>
    </body>
    </html>
    """


@app.get("/health")
def health():
    return {"status": "running", "application": "AttendIQ", "version": "1.0.0"}


@app.get("/api/students")
def students(
    limit: int = Query(default=200, ge=1, le=500),
    _staff: dict = Depends(require_staff),
):
    """Return existing student directory entries to authenticated staff only."""
    return {"students": list_students(limit)}


@app.post("/api/students/{student_id}/face-photo", status_code=201)
async def add_student_face_photo(
    student_id: int,
    photo: UploadFile = File(...),
    student_consented: bool = Form(...),
    _staff: dict = Depends(require_staff),
):
    """Add a staff-captured phone photo to an existing student's face profile."""
    if not student_consented:
        raise HTTPException(status_code=400, detail="Confirm that the student agrees before saving a face photo.")
    student = get_student(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student record not found.")
    image_bytes = await photo.read(4 * 1024 * 1024 + 1)
    await photo.close()
    if len(image_bytes) > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Photo is too large. Please take another photo.")
    try:
        normalized_photo = prepare_face_registration_photo(image_bytes)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    image_path = None
    try:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        image_path = IMAGE_DIR / f"{student_id}_mobile_{stamp}.jpg"
        with image_path.open("xb") as saved_photo:
            saved_photo.write(normalized_photo)

        connection = sqlite3.connect(desktop_auth.DB_PATH, timeout=5)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "activity_log" not in tables:
                raise RuntimeError("AttendIQ activity log has not been initialized.")
            with connection:
                connection.execute(
                    "INSERT INTO activity_log(event_time, actor, action, details) VALUES (?, ?, ?, ?)",
                    (datetime.now().astimezone().isoformat(timespec="seconds"),
                     _staff["username"], "WEB_FACE_SAMPLE_ADDED",
                     f"Added phone face sample for student {student_id}"),
                )
        finally:
            connection.close()
    except Exception as error:
        if image_path is not None:
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(status_code=503, detail="Could not save the photo. Check that AttendIQ is running on the PC.") from error

    return {
        "status": "saved",
        "student_id": student_id,
        "student_name": student["name"],
        "message": f"A new face photo was added for {student['name']}. Previous photos were kept.",
    }

class SessionRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=100)
    duration_minutes: int = Field(default=15, ge=1, le=60)
    test_mode: bool = False
    academic_year: str = Field(default="", max_length=50)
    department: str = Field(default="", max_length=50)
    class_name: str = Field(default="", max_length=50)


@app.get("/api/sessions")
def get_active_sessions(_staff: dict = Depends(require_staff)):
    """List active sessions visible to this authenticated staff account."""
    return {"sessions": session_store.list_active_sessions(_staff)}


@app.post("/api/sessions", status_code=201)
def start_session(
    details: SessionRequest,
    request: Request,
    _staff: dict = Depends(require_staff),
):
    """Start a temporary teacher-controlled session at the configured campus."""
    try:
        public_base = teacher_public_base_url()
        client_host = request.client.host if request.client else ""
        is_local = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
        if is_local and not public_base:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The secure student phone link is still starting. Wait for the Cloudflare link, then try again.",
            )
        session = session_store.create_session(
            details.subject, details.duration_minutes, _staff,
            test_mode=details.test_mode,
            academic_year=details.academic_year,
            department=details.department,
            class_name=details.class_name,
        )
        attendance_url = (public_base or str(request.base_url).rstrip("/")) + "/attend#" + session.pop("qr_token")
        image = qrcode.make(attendance_url)
        image_buffer = BytesIO()
        image.save(image_buffer, format="PNG")
        session["attendance_url"] = attendance_url
        session["qr_code_png_base64"] = base64.b64encode(image_buffer.getvalue()).decode("ascii")
        return session
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/sessions/{session_id}/close")
def end_session(session_id: int, _staff: dict = Depends(require_staff)):
    """Close the requesting teacher's active session; Admins may close any."""
    if session_id <= 0:
        raise HTTPException(status_code=422, detail="session_id must be positive.")
    if not session_store.close_session(session_id, _staff):
        raise HTTPException(status_code=404, detail="Active session not found.")
    return {"status": "closed", "session_id": session_id}




class QRResolveRequest(BaseModel):
    token: str = Field(min_length=32, max_length=128)


@app.get("/attend", response_class=HTMLResponse)
def attend_page():
    """Guide students through location and blink-based face verification."""
    return """
    <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>AttendIQ Check-in</title><style>
    body{font:16px Arial;background:#07101f;color:#fff;padding:18px}main{max-width:480px;margin:5vh auto;background:#101d32;padding:24px;border-radius:16px}
    h1{color:#00c8f0}p,label{color:#b8c7dc}button{background:#00c8f0;border:0;border-radius:8px;padding:12px 16px;font-weight:bold;margin:8px 0}
    button:disabled{opacity:.5}input{display:block;width:95%;padding:12px;margin:8px 0 16px;border-radius:6px;border:1px solid #456;color:#111}
    video{display:block;width:100%;max-height:62vh;object-fit:contain;border-radius:12px;background:#000;margin-top:10px}
    </style></head><body><main><h1>AttendIQ</h1><div id="message">Checking this attendance session…</div>
    <button id="locationButton" disabled>Step 1: Check campus location</button><p id="locationResult"></p>
    <section id="facePanel" style="display:none"><label for="studentId">Your student ID</label>
    <input id="studentId" type="number" min="1" inputmode="numeric" placeholder="Enter your student ID">
    <p>Tap Step 2 to open the selfie-camera preview. Center only the student’s face in bright even light. When ready, tap Start face capture, keep the phone still, and blink once slowly.</p>
    <video id="camera" autoplay playsinline muted style="display:none"></video>
    <button id="faceButton">Step 2: Verify my face</button><button id="cancelCameraButton" type="button" style="display:none">Close camera</button><p id="faceResult"></p></section>
    <p id="attendanceNotice">Attendance is recorded only after the location, student ID, face and blink checks pass.</p>
    <script>
    (async()=>{const token=location.hash.slice(1);history.replaceState(null,"",location.pathname);
    const box=document.getElementById("message"),locationButton=document.getElementById("locationButton"),locationResult=document.getElementById("locationResult");
    const facePanel=document.getElementById("facePanel"),faceButton=document.getElementById("faceButton"),faceResult=document.getElementById("faceResult"),video=document.getElementById("camera"),cancelCameraButton=document.getElementById("cancelCameraButton");
    let liveStream=null;
    function stopLiveCamera(){if(liveStream)liveStream.getTracks().forEach(track=>track.stop());liveStream=null;video.srcObject=null;video.style.display="none";cancelCameraButton.style.display="none";cancelCameraButton.disabled=false;}
    cancelCameraButton.addEventListener("click",()=>{stopLiveCamera();faceButton.disabled=false;faceButton.textContent="Step 2: Verify my face";faceResult.textContent="Camera closed. Tap Step 2 when ready.";});
    if(!token){box.textContent="This QR code is incomplete.";return;}
    try{const response=await fetch("/api/sessions/resolve-qr",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token})});
    const session=await response.json();if(!response.ok)throw new Error(session.detail||"This QR code is no longer active.");
    box.innerHTML="<h2></h2><p></p>";box.querySelector("h2").textContent=(session.test_mode?"TEST — ":"")+session.subject;const group=[session.academic_year,session.department,session.class_name].filter(Boolean).join(" · ")||"All students";box.querySelector("p").textContent=group+" · Expires: "+session.expires_at;if(session.test_mode){document.getElementById("attendanceNotice").textContent="TEST ONLY: Successful checks are not saved as attendance.";const note=document.createElement("p");note.textContent="This temporary test session uses the test area.";box.append(note);}locationButton.disabled=false;
    const describeLocationError=error=>error&&error.code===1?"Location permission was blocked. Allow it in your browser settings.":error&&error.code===2?"Your device could not determine its location. Check device location services.":error&&error.code===3?"Location took too long. Try again outdoors or use a device with GPS.":(error&&error.message)||"Location could not be checked.";
    const getPosition=()=>new Promise((resolve,reject)=>navigator.geolocation.getCurrentPosition(resolve,reject,{enableHighAccuracy:true,timeout:30000,maximumAge:0}));
    locationButton.addEventListener("click",async()=>{locationButton.disabled=true;locationResult.textContent="Checking your location…";
    try{if(!navigator.geolocation)throw new Error("Location is not available in this browser.");const position=await getPosition();
    const check=await fetch("/api/sessions/check-location",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token,latitude:position.coords.latitude,longitude:position.coords.longitude,accuracy_meters:position.coords.accuracy})});
    const result=await check.json();if(!check.ok)throw new Error(result.detail||"Location check failed.");
    locationResult.textContent=result.within_geofence?"Location is within the campus radius.":result.reason==="location_accuracy_low"?"GPS accuracy is too low. Try again outdoors.":"Location is outside the campus radius.";
    if(result.within_geofence)facePanel.style.display="block";
    }catch(error){locationResult.textContent=describeLocationError(error);}finally{locationButton.disabled=false;}});
    faceButton.addEventListener("click",async()=>{
    const studentId=document.getElementById("studentId").value;
    if(!studentId||Number(studentId)<=0){faceResult.textContent="Enter your student ID first.";return;}
    if(!liveStream){
      faceButton.disabled=true;faceResult.textContent="Opening your selfie camera…";
      try{if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia)throw new Error("Camera access requires HTTPS or localhost.");
      liveStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:"user",width:{ideal:1280},height:{ideal:960}},audio:false});
      video.srcObject=liveStream;video.style.display="block";await video.play();await new Promise(resolve=>setTimeout(resolve,1200));
      faceResult.textContent="Camera ready. Center one student’s face, hold steady, and check the preview. Tap Start face capture when ready.";
      cancelCameraButton.style.display="inline-block";faceButton.textContent="Start face capture";faceButton.disabled=false;
      }catch(error){stopLiveCamera();faceResult.textContent=error&&error.message?error.message:"Could not open the camera. Check browser camera permission.";faceButton.disabled=false;faceButton.textContent="Step 2: Verify my face";}
      return;
    }
    faceButton.disabled=true;cancelCameraButton.disabled=true;faceResult.textContent="Capturing 16 face samples. Keep your face centered and blink once slowly when prompted…";
    try{const canvas=document.createElement("canvas"),context=canvas.getContext("2d");
    if(!video.videoWidth||!video.videoHeight)throw new Error("Camera is not ready. Close and reopen it.");
    const scale=Math.min(960/video.videoWidth,960/video.videoHeight,1);canvas.width=Math.max(80,Math.round(video.videoWidth*scale));canvas.height=Math.max(80,Math.round(video.videoHeight*scale));
    const form=new FormData();form.append("token",token);form.append("student_id",studentId);
    for(let i=0;i<16;i++){if(i===5)faceResult.textContent="Blink once slowly now. Keep your face centered and your head still…";await new Promise(resolve=>setTimeout(resolve,250));context.drawImage(video,0,0,canvas.width,canvas.height);let blob=await new Promise(resolve=>canvas.toBlob(resolve,"image/jpeg",0.55));if(!blob)throw new Error("Could not capture a camera frame.");while(blob.size>220*1024&&canvas.width>320){canvas.width=Math.round(canvas.width*0.85);canvas.height=Math.round(canvas.height*0.85);context.drawImage(video,0,0,canvas.width,canvas.height);blob=await new Promise(resolve=>canvas.toBlob(resolve,"image/jpeg",0.45));if(!blob)throw new Error("Could not capture a camera frame.");}if(blob.size>220*1024)throw new Error("Camera frame is too large. Try again.");form.append("frames",blob,"frame"+i+".jpg");}
    if(liveStream)liveStream.getTracks().forEach(track=>track.stop());liveStream=null;video.srcObject=null;video.style.display="none";cancelCameraButton.style.display="none";
    const position=await getPosition();form.append("latitude",position.coords.latitude);form.append("longitude",position.coords.longitude);form.append("accuracy_meters",position.coords.accuracy);
    faceResult.textContent="Checking your face and location…";const check=await fetch("/api/sessions/verify-face",{method:"POST",body:form});const result=await check.json();
    if(!check.ok)throw new Error(result.detail||"Face check could not be completed.");faceResult.textContent=result.message;
    }catch(error){faceResult.textContent=error&&error.code?describeLocationError(error):(error.message||"Face check could not be completed. Try again.");}
    finally{stopLiveCamera();cancelCameraButton.disabled=false;faceButton.disabled=false;faceButton.textContent="Step 2: Verify my face";}});
    }catch(error){box.textContent=error.message||"This QR code is no longer active.";}})();</script></main></body></html>
    """

@app.post("/api/sessions/resolve-qr")
def resolve_qr(details: QRResolveRequest, request: Request):
    """Resolve a short-lived QR token without exposing student records."""
    client_host = request.client.host if request.client else ""
    is_local = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
    if request.url.scheme != "https" and not is_local:
        raise HTTPException(status_code=400, detail="HTTPS is required for remote QR scans.")
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("origin") != expected_origin:
        raise HTTPException(status_code=403, detail="Request origin is not allowed.")
    session = session_store.resolve_qr_token(details.token)
    if session is None:
        raise HTTPException(status_code=404, detail="This QR code is no longer active.")
    return session





class LocationRequest(BaseModel):
    token: str = Field(min_length=32, max_length=128)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_meters: float = Field(ge=0, le=10000)


@app.post("/api/sessions/check-location")
def check_location(details: LocationRequest, request: Request):
    """Check reported campus distance without recording attendance or coordinates."""
    client_host = request.client.host if request.client else ""
    is_local = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
    if request.url.scheme != "https" and not is_local:
        raise HTTPException(status_code=400, detail="HTTPS is required for remote location checks.")
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("origin") != expected_origin:
        raise HTTPException(status_code=403, detail="Request origin is not allowed.")
    try:
        result = session_store.check_location(
            details.token, details.latitude, details.longitude, details.accuracy_meters
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="This QR code is no longer active.")
    return result


@app.post("/api/sessions/verify-face")
async def verify_face(
    request: Request,
    token: str = Form(..., min_length=32, max_length=128),
    student_id: int = Form(..., gt=0),
    latitude: float = Form(..., ge=-90, le=90),
    longitude: float = Form(..., ge=-180, le=180),
    accuracy_meters: float = Form(..., ge=0, le=10000),
    frames: list[UploadFile] = File(...),
):
    """Verify a claimed student face, blink and campus location without recording attendance."""
    client_host = request.client.host if request.client else "unknown"
    is_local = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
    if request.url.scheme != "https" and not is_local:
        raise HTTPException(status_code=400, detail="HTTPS is required for remote identity checks.")
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("origin") != expected_origin:
        raise HTTPException(status_code=403, detail="Request origin is not allowed.")
    session = session_store.resolve_qr_token(token)
    if session is None:
        raise HTTPException(status_code=404, detail="This QR code is no longer active.")
    student = get_student(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student record could not be verified.")
    if (session.get("academic_year") and (student.get("year") or "").strip().casefold()
            != session["academic_year"].strip().casefold()):
        raise HTTPException(status_code=403, detail="This QR code is for a different academic year.")
    if (session.get("department") and (student.get("department") or "").strip().casefold()
            != session["department"].strip().casefold()):
        raise HTTPException(status_code=403, detail="This QR code is for a different department.")
    if (session.get("class_name") and (student.get("class_name") or "").strip().casefold()
            != session["class_name"].strip().casefold()):
        raise HTTPException(status_code=403, detail="This QR code is for a different class or section.")
    location = session_store.check_location(token, latitude, longitude, accuracy_meters)
    if not location or not location["within_geofence"]:
        raise HTTPException(status_code=403, detail="Campus location check did not pass.")
    throttle_key = f"{client_host}:{session['id']}"
    if _face_throttle.locked_for(throttle_key):
        raise HTTPException(status_code=429, detail="Too many attempts. Wait before trying again.")
    if not 4 <= len(frames) <= 16:
        raise HTTPException(status_code=422, detail="Capture 4 to 16 face frames, including one blink.")
    frame_bytes = []
    total_bytes = 0
    try:
        for frame in frames:
            content = await frame.read(512 * 1024 + 1)
            await frame.close()
            total_bytes += len(content)
            if len(content) > 512 * 1024 or total_bytes > 4 * 1024 * 1024:
                _face_throttle.record_failure(throttle_key)
                return {"verified": False, "message": "Face capture was too large. Reopen the camera, move into good light, and try again."}
            frame_bytes.append(content)
        verified = verify_student_liveness(student_id, frame_bytes)
    finally:
        for frame in frames:
            await frame.close()
        frame_bytes.clear()
    if not verified:
        _face_throttle.record_failure(throttle_key)
        return {
            "verified": False,
            "message": "Face check did not pass. Keep one face centered in good light and blink once slowly when prompted.",
        }
    _face_throttle.reset(throttle_key)
    if session.get("test_mode"):
        return {
            "verified": True,
            "recorded": False,
            "test_mode": True,
            "message": "Test passed. No attendance was recorded.",
        }
    attendance = mark_web_attendance(
        student_id, session["id"], location["distance_meters"],
        location["accuracy_meters"],
    )
    if attendance["status"] == "student_not_found":
        return {"verified": False, "message": "Student record could not be verified."}
    if attendance["status"] == "already_recorded":
        return {
            "verified": True,
            "recorded": False,
            "message": "Attendance was already recorded today. No duplicate was created.",
        }
    response = {
        "verified": True,
        "recorded": True,
        "message": "Attendance recorded successfully.",
    }
    if attendance.get("security_warning"):
        response["security_warning"] = attendance["security_warning"]
    return response





@app.get("/teacher", response_class=HTMLResponse)
def teacher_dashboard(_staff: dict = Depends(require_staff)):
    """Simple staff page for starting sessions and displaying their QR codes."""
    return """
    <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>AttendIQ Teacher</title><style>
    body{font:16px Arial;background:#07101f;color:#fff;padding:18px}main{max-width:760px;margin:4vh auto;background:#101d32;padding:24px;border-radius:16px}
    h1{color:#00c8f0}p{color:#b8c7dc}label{display:block;margin-top:14px}input,select{box-sizing:border-box;width:100%;padding:12px;margin-top:6px;border-radius:6px;border:1px solid #456}
    button{background:#00c8f0;border:0;border-radius:8px;padding:12px 16px;font-weight:bold;margin:10px 8px 8px 0;cursor:pointer}
    button:disabled{opacity:.5}.card{border:1px solid #345;border-radius:12px;padding:16px;margin:14px 0}.qr{width:240px;height:240px;background:#fff;padding:8px;display:block}
    .link{word-break:break-all;color:#9ee8ff}.muted{color:#91a9c8;font-size:14px}
    </style></head><body><main><h1>AttendIQ — Teacher</h1>
    <p>Campus sessions use the real campus area. Temporary test sessions use your test location and never save attendance.</p>
    <form id="sessionForm"><label for="subject">Subject or session name</label><input id="subject" maxlength="100" required placeholder="e.g. Computer Science">
    <label for="sessionYear">Academic year (optional)</label><select id="sessionYear"><option value="">All years</option></select>
    <label for="sessionDepartment">Department (optional)</label><select id="sessionDepartment"><option value="">All departments</option></select>
    <label for="sessionClass">Class / section (optional)</label><select id="sessionClass"><option value="">All classes</option></select>
    <label for="duration">Link duration</label><select id="duration"><option value="15" selected>15 minutes</option><option value="60">60 minutes — testing</option></select>
    <label for="testMode">Session type</label><select id="testMode"><option value="false" selected>Campus attendance</option><option value="true">Temporary test location — no attendance saved</option></select>
    <button id="createButton" type="submit">Start session and show QR</button></form><p id="message" role="status"></p>
    <section id="qrCard" class="card" style="display:none"><h2 id="sessionTitle"></h2><p id="sessionExpiry"></p><img id="qrImage" class="qr" alt="Temporary attendance QR code">
    <p class="muted">Keep this QR and its link private. This local test link works only on this computer.</p>
    <p><a id="attendanceLink" class="link"></a></p><button id="copyButton" type="button">Copy student link</button></section>
    <section class="card"><h2>Update a student face photo</h2>
    <p class="muted">Teachers and Admins can use this phone camera tool to add a clear face sample to an existing student record. Ask the student for permission first. The old face samples are kept.</p>
    <label for="photoStudent">Student</label><select id="photoStudent"><option value="">Loading students…</option></select>
    <label style="display:flex;align-items:flex-start;gap:8px"><input id="photoConsent" type="checkbox" style="width:auto;margin-top:3px">I confirm the student agrees to have this photo saved for face attendance.</label>
    <label for="photoCameraFacing">Phone camera</label><select id="photoCameraFacing"><option value="user" selected>Selfie camera (front)</option><option value="environment">Rear camera</option></select>
    <button id="photoOpenButton" type="button">Open selected camera</button>
    <button id="photoCaptureButton" type="button" style="display:none">Take photo</button>
    <button id="photoRetakeButton" type="button" style="display:none">Retake photo</button>
    <button id="photoSaveButton" type="button" disabled>Save face sample</button>
    <video id="photoCamera" autoplay playsinline muted style="display:none;width:100%;max-height:420px;object-fit:contain;background:#000;border-radius:12px"></video>
    <img id="photoPreview" alt="Face photo preview" style="display:none;width:100%;max-height:420px;object-fit:contain;background:#000;border-radius:12px">
    <p id="photoMessage" role="status" class="muted"></p></section>    <section class="card"><h2>Active sessions</h2><div id="sessionList" class="muted">Loading…</div></section>
    <script>
    const message=document.getElementById("message"),form=document.getElementById("sessionForm"),createButton=document.getElementById("createButton");
    const qrCard=document.getElementById("qrCard"),qrImage=document.getElementById("qrImage"),attendanceLink=document.getElementById("attendanceLink");
    async function api(path,options={}){const response=await fetch(path,{credentials:"same-origin",...options});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.detail||"Request failed ("+response.status+")");return data;}
    async function refreshSessions(){const list=document.getElementById("sessionList");try{const data=await api("/api/sessions");list.replaceChildren();if(!data.sessions.length){list.textContent="No active sessions.";return;}
    data.sessions.forEach(session=>{const card=document.createElement("div");card.className="card";const group=[session.academic_year,session.department,session.class_name].filter(Boolean).join(" · ")||"All students";const title=document.createElement("strong");title.textContent=(session.test_mode?"TEST ONLY — ":"")+session.subject+" — "+group+" — expires "+session.expires_at;const close=document.createElement("button");close.textContent="Close session";close.addEventListener("click",async()=>{try{await api("/api/sessions/"+session.id+"/close",{method:"POST"});message.textContent="Session closed.";refreshSessions();}catch(error){message.textContent=error.message;}});card.append(title,close);list.append(card);});
    }catch(error){list.textContent=error.message;}}
    form.addEventListener("submit",async event=>{event.preventDefault();createButton.disabled=true;message.textContent="Starting session…";
    try{const data=await api("/api/sessions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({subject:document.getElementById("subject").value,academic_year:document.getElementById("sessionYear").value,department:document.getElementById("sessionDepartment").value,class_name:document.getElementById("sessionClass").value,duration_minutes:Number(document.getElementById("duration").value),test_mode:document.getElementById("testMode").value==="true"})});
    const group=[data.academic_year,data.department,data.class_name].filter(Boolean).join(" · ")||"All students";document.getElementById("sessionTitle").textContent=(data.test_mode?"TEST ONLY — ":"")+data.subject+" · "+group;document.getElementById("sessionExpiry").textContent="Expires: "+data.expires_at;qrImage.src="data:image/png;base64,"+data.qr_code_png_base64;
    attendanceLink.href=data.attendance_url;attendanceLink.textContent=data.attendance_url;qrCard.style.display="block";message.textContent=data.test_mode?"Test session started. Attendance will not be recorded.":"Campus session started. QR code is ready.";refreshSessions();
    }catch(error){message.textContent=error.message;}finally{createButton.disabled=false;}});
    document.getElementById("copyButton").addEventListener("click",async()=>{try{await navigator.clipboard.writeText(attendanceLink.href);message.textContent="Student link copied. Keep it private.";}catch(error){message.textContent="Copy was blocked; select the student link above and copy it.";}});
    const photoStudent=document.getElementById("photoStudent"),photoCamera=document.getElementById("photoCamera"),photoPreview=document.getElementById("photoPreview"),photoMessage=document.getElementById("photoMessage");
    let photoStream=null,capturedPhoto=null,photoPreviewUrl=null;
    function stopPhotoCamera(){if(photoStream)photoStream.getTracks().forEach(track=>track.stop());photoStream=null;photoCamera.srcObject=null;}
    async function loadPhotoStudents(){try{const data=await api("/api/students?limit=500");photoStudent.replaceChildren(new Option("Choose a student", ""));data.students.forEach(student=>{const option=document.createElement("option");option.value=student.id;option.textContent=student.id+" — "+student.name;photoStudent.append(option);});}catch(error){photoStudent.replaceChildren(new Option("Student list could not be loaded", ""));photoMessage.textContent=error.message;}}
    async function openPhotoCamera(){if(!photoStudent.value){photoMessage.textContent="Choose the existing student first.";return;}if(!document.getElementById("photoConsent").checked){photoMessage.textContent="Confirm the student's permission before opening the camera.";return;}try{stopPhotoCamera();if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia)throw new Error("Camera access requires HTTPS or localhost.");const facingMode=document.getElementById("photoCameraFacing").value;photoStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:facingMode},width:{ideal:1280},height:{ideal:720}},audio:false});photoCamera.srcObject=photoStream;photoCamera.style.display="block";await photoCamera.play();document.getElementById("photoOpenButton").style.display="none";document.getElementById("photoCaptureButton").style.display="inline-block";document.getElementById("photoCaptureButton").disabled=false;photoMessage.textContent="Center one student's face in good light, then take the photo.";}catch(error){stopPhotoCamera();photoMessage.textContent=error.message||"Could not open the phone camera. Check browser camera permission.";}}
    document.getElementById("photoOpenButton").addEventListener("click",openPhotoCamera);
    document.getElementById("photoCaptureButton").addEventListener("click",async()=>{try{const width=photoCamera.videoWidth,height=photoCamera.videoHeight;if(!width||!height)throw new Error("Camera is not ready. Try again.");const scale=Math.min(1280/width,1280/height,1),canvas=document.createElement("canvas");canvas.width=Math.round(width*scale);canvas.height=Math.round(height*scale);canvas.getContext("2d").drawImage(photoCamera,0,0,canvas.width,canvas.height);const blob=await new Promise(resolve=>canvas.toBlob(resolve,"image/jpeg",0.92));if(!blob)throw new Error("Could not capture the photo. Try again.");capturedPhoto=blob;if(photoPreviewUrl)URL.revokeObjectURL(photoPreviewUrl);photoPreviewUrl=URL.createObjectURL(blob);photoPreview.src=photoPreviewUrl;photoPreview.style.display="block";photoCamera.style.display="none";stopPhotoCamera();document.getElementById("photoCaptureButton").style.display="none";document.getElementById("photoRetakeButton").style.display="inline-block";document.getElementById("photoSaveButton").disabled=false;photoMessage.textContent="Check the preview. Save only if it clearly shows the chosen student.";}catch(error){photoMessage.textContent=error.message||"Could not capture the photo.";}});
    document.getElementById("photoRetakeButton").addEventListener("click",async()=>{capturedPhoto=null;photoPreview.style.display="none";document.getElementById("photoSaveButton").disabled=true;document.getElementById("photoRetakeButton").style.display="none";document.getElementById("photoOpenButton").style.display="inline-block";await openPhotoCamera();});
    document.getElementById("photoSaveButton").addEventListener("click",async()=>{if(!capturedPhoto){photoMessage.textContent="Take a photo first.";return;}if(!document.getElementById("photoConsent").checked){photoMessage.textContent="Confirm the student's permission before saving.";return;}const studentId=photoStudent.value;if(!studentId){photoMessage.textContent="Choose the existing student first.";return;}const formData=new FormData();formData.append("photo",capturedPhoto,"face-sample.jpg");formData.append("student_consented","true");const saveButton=document.getElementById("photoSaveButton");saveButton.disabled=true;photoMessage.textContent="Checking and saving the face photo…";try{const response=await fetch("/api/students/"+encodeURIComponent(studentId)+"/face-photo",{method:"POST",body:formData,credentials:"same-origin"});const result=await response.json().catch(()=>({}));if(!response.ok)throw new Error(result.detail||"Photo could not be saved.");photoMessage.textContent=result.message;capturedPhoto=null;photoPreview.style.display="none";document.getElementById("photoRetakeButton").style.display="none";document.getElementById("photoOpenButton").style.display="inline-block";document.getElementById("photoConsent").checked=false;}catch(error){photoMessage.textContent=error.message||"Photo could not be saved. Try again.";saveButton.disabled=false;}});
    async function loadSessionGroups(){try{const data=await api("/api/students?limit=500"),students=data.students;const year=document.getElementById("sessionYear"),department=document.getElementById("sessionDepartment"),className=document.getElementById("sessionClass");const fill=(select,first,values)=>{const old=select.value;select.replaceChildren(new Option(first,""));values.filter(Boolean).sort((a,b)=>a.localeCompare(b)).forEach(value=>select.add(new Option(value,value)));if([...select.options].some(option=>option.value===old))select.value=old;};const update=()=>{fill(department,"All departments",[...new Set(students.filter(s=>!year.value||s.year===year.value).map(s=>s.department||""))]);const scoped=students.filter(s=>(!year.value||s.year===year.value)&&(!department.value||s.department===department.value));fill(className,"All classes",[...new Set(scoped.map(s=>s.class_name||""))]);};fill(year,"All years",[...new Set(students.map(s=>s.year||""))]);update();year.addEventListener("change",update);department.addEventListener("change",()=>{const scoped=students.filter(s=>(!year.value||s.year===year.value)&&(!department.value||s.department===department.value));fill(className,"All classes",[...new Set(scoped.map(s=>s.class_name||""))]);});}catch(error){message.textContent="Class filters could not load: "+error.message;}}
    loadSessionGroups();loadPhotoStudents();refreshSessions();
    </script></main></body></html>
    """


