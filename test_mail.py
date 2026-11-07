   import smtplib, ssl, getpass
   from email.message import EmailMessage

   sender = input("Gmail address: ").strip()
   password = getpass.getpass("App password: ").replace(" ", "")
   s = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20)
   s.login(sender, password)
   msg = EmailMessage()
   msg["Subject"] = "AttendIQ test"
   msg["From"] = sender
   msg["To"] = sender
   msg.set_content("It works.")
   s.send_message(msg)
   print("SENT")
   s.quit()