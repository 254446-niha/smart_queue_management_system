# SmartQueue — Smart Queue Management System

A B.Tech project demonstrating Python, DSA, SQLite/DBMS, HTML/CSS/JavaScript and OpenCV.

## Main features
- One login/registration page for customers and administrators
- Customer mobile number collection
- Optional real SMS notifications through Twilio
- Digital tokens for General, Billing, Support and Registration
- Normal FIFO queue + priority heap (Emergency > Priority > Normal)
- Dynamic waiting-time estimate
- Multiple service counters
- Admin dashboard and queue analytics
- User-friendly live queue display (raw JSON is not shown to customers)
- OpenCV pedestrian-counting module for crowd monitoring

## Run in VS Code
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000

## Demo admin
Email: `admin@queue.com`
Password: `admin123`

Admin registration code: `ADMIN2026`

## Real SMS setup
1. Create/configure a Twilio account and obtain a messaging-capable sender/number according to the current rules for your destination country.
2. Copy `.env.example` to `.env`.
3. Put your own Twilio credentials in `.env`:

```text
SECRET_KEY=replace-with-a-random-secret
TWILIO_ACCOUNT_SID=your_account_sid
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_PHONE_NUMBER=your_twilio_sender
```

Never commit `.env` to GitHub and never share your Auth Token.

The app sends two SMS events for an SMS-enabled customer:
- **Near turn:** when there are 2 or fewer customers ahead.
- **Turn:** when an admin calls the token.

The `notification_log` table prevents duplicate SMS for the same event.

## OpenCV
Run the pedestrian counter separately:
```powershell
python cv/people_counter.py
```
It uses OpenCV's built-in HOG pedestrian detector and your laptop camera.
