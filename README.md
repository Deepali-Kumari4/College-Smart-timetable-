SmartTimetable ERP
SmartTimetable ERP is a role-based college timetable management system with:

-Admin approval workflows
-Teacher preference submission
-Optimization-based timetable generation
-Student/Teacher personalized timetable views
-Academic calendar and event management

Roles and Key Features
Admin:
Approve/reject teacher and student signup requests
Approve/reject/edit teacher preference requests
See Generation Stack of approved course preferences
Generate timetable semester-wise
Edit/delete timetable rows before generation
View semester-wise timetable generation history
Manage events and vacations
Teacher:
Signup/login after admin approval
Submit subject preferences (day/slot/target department)
View pending/approved preference status
View personal timetable
Manage own calendar events
Student:
Signup/login after admin approval
View department-based personal timetable
View institute timetable with filters
View academic calendar and event details

How to Start the Project
1. Open terminal in project folder
cd /d c:\Users\LENOVO\Desktop\Code\SmartTimetable
2. Install dependencies (first time only)
pip install flask pulp
3. Run the server
python app.py
4. Open browser
http://127.0.0.1:5000/login

If port 5000 is busy:
set PORT=5050
python app.py
Then open:
http://127.0.0.1:5050/login
