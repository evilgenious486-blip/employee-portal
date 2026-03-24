# Employee Portal Web Application v3

Ready-to-use Flask employee portal with role-based access and employee self service.

## Included modules
- Role-based login
- Employee profile
- Leave application and tracking
- Manager and HR approval flow
- Searchable employee directory
- Employee detail pages
- Add employee
- Edit employee
- Deactivate employee account
- Attendance module
- Payslip module
- Document upload and listing
- Notifications center
- Password change and forgot-password flow
- Admin email center with queued portal emails
- Admin settings
- Audit logs

## Demo logins
- Employee: `employee@example.com` / `Employee@123`
- Manager: `manager@example.com` / `Manager@123`
- HR: `hr@example.com` / `HR@12345`
- Admin: `admin@example.com` / `Admin@123`

## Run locally
```bash
pip install -r requirements.txt
python app.py
```

Open in browser:
```bash
http://127.0.0.1:5000
```

## Reset demo database
Open this URL once after starting the app:
```bash
http://127.0.0.1:5000/initdb
```

## Notes
- Employee deletion is implemented as **safe deactivation** so historical leave, payroll, and audit records stay intact.
- Email notifications are stored in the built-in **Email Center**. This is ready for later SMTP integration if you want real email sending.


## Employee Login IDs
Use the employee PAC code as the login ID. Example: `PAC-249`

- PAC-249 / Muhammad@123
- PAC-127 / Faisal@123
- PAC-424 / Mobeen@123
- PAC-450 / Qazi@123
- PAC-452 / Usama@123
- PAC-455 / Abid@123
- PAC-421 / Shabir@123
- PAC-457 / Sagir@123
- PAC-295 / Ashfaq@123
- PAC-462 / Vijendra@123
- PAC-313 / Salahuddin@123
- PAC-224 / Mirza@123
- PAC-324 / MuhammadAshfaq@123
- PAC-476 / Sayem@123
- PAC-190 / Suhel@123
- PAC-376 / Adnan@123
- PAC-378 / Umair@123
- PAC-464 / Ghanem@123

After updating to this version, run `/initdb` once to rebuild the database.
