from fastapi import FastAPI, Request, Response, BackgroundTasks, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import mysql.connector
from mysql.connector import errorcode
import datetime
import os
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from jinja2 import Template
import time
import threading
import json
import urllib.parse

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutoMailBackend")

app = FastAPI(title="AutoMail Cloud Service (MySQL)")

# Database configuration
# Set this in your Aiven / Cloud provider environment variables
# Example: mysql://user:pass@host:port/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "mysql://user:pass@host:port/dbname")

def get_db_conn():
    # Parse MySQL URL
    url = urllib.parse.urlparse(DATABASE_URL)
    return mysql.connector.connect(
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port or 3306,
        database=url.path.lstrip('/')
    )

def init_db():
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        
        # Events table for tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INT AUTO_INCREMENT PRIMARY KEY,
                campaign_id VARCHAR(255),
                customer_id VARCHAR(255),
                action VARCHAR(50),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ip VARCHAR(45),
                user_agent TEXT,
                target_url TEXT
            )
        ''')
        
        # Scheduled campaigns table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_campaigns (
                id VARCHAR(255) PRIMARY KEY,
                schedule_time DATETIME,
                title TEXT,
                template TEXT,
                promo_data JSON,
                customers JSON,
                promo_products JSON,
                promo_bundles JSON,
                status VARCHAR(50) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at DATETIME,
                error TEXT
            )
        ''')
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("MySQL Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize MySQL database: {e}")

# Call init_db on startup
@app.on_event("startup")
async def startup_event():
    init_db()
    # Start the background worker thread
    thread = threading.Thread(target=scheduler_worker, daemon=True)
    thread.start()

# --- SCHEDULING MODELS ---
class ScheduledCampaign(BaseModel):
    id: str
    schedule_time: str
    title: str
    template: str
    promo_data: dict
    customers: list
    promo_products: list
    promo_bundles: list

# --- BACKGROUND WORKER ---
def scheduler_worker():
    """Background worker that polls for pending campaigns every minute"""
    logger.info("Background scheduler worker started.")
    while True:
        try:
            process_pending_campaigns()
        except Exception as e:
            logger.error(f"Error in scheduler worker: {e}")
        time.sleep(60)

def process_pending_campaigns():
    conn = get_db_conn()
    cursor = conn.cursor(dictionary=True)
    
    # Get pending campaigns whose time has passed
    cursor.execute("""
        SELECT * FROM scheduled_campaigns 
        WHERE status = 'pending' AND schedule_time <= NOW()
    """)
    pending = cursor.fetchall()
    
    for item in pending:
        logger.info(f"Processing scheduled campaign: {item['title']}")
        
        # Mark as processing
        cursor.execute("UPDATE scheduled_campaigns SET status = 'processing' WHERE id = %s", (item['id'],))
        conn.commit()
        
        try:
            send_campaign(item)
            # Delete if successful to keep DB clean
            cursor.execute("DELETE FROM scheduled_campaigns WHERE id = %s", (item['id'],))
        except Exception as e:
            logger.error(f"Failed to process campaign {item['id']}: {e}")
            cursor.execute("UPDATE scheduled_campaigns SET status = 'failed', error = %s, processed_at = NOW() WHERE id = %s", (str(e), item['id']))
        
        conn.commit()
    
    cursor.close()
    conn.close()

def send_campaign(item):
    """Actual sending logic moved to the cloud"""
    # MySQL returns JSON columns as strings or dicts depending on connector config
    # In mysql-connector, it's usually a string if not handled
    promo_data = item['promo_data']
    if isinstance(promo_data, str): promo_data = json.loads(promo_data)
    
    customers = item['customers']
    if isinstance(customers, str): customers = json.loads(customers)
    
    # SMTP Settings (Should be provided in environment variables)
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", 465))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    from_email = os.getenv("FROM_EMAIL", smtp_user)
    company_name = os.getenv("COMPANY_NAME", "AutoMail")
    
    if not all([smtp_server, smtp_user, smtp_pass]):
        raise Exception("SMTP credentials not configured in environment variables.")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_server, smtp_port, context=context) as server:
        server.login(smtp_user, smtp_pass)
        
        for customer in customers:
            email = customer.get("email")
            if not email: continue
            
            msg = MIMEMultipart("alternative")
            msg["Subject"] = promo_data.get("title", "Special Promotion")
            msg["From"] = f"{company_name} <{from_email}>"
            msg["To"] = email
            
            body_text = f"Hi {customer.get('name', 'Customer')},\n\n{promo_data.get('description', '')}"
            msg.attach(MIMEText(body_text, "plain"))
            server.sendmail(smtp_user, email, msg.as_string())

# --- API ENDPOINTS ---
@app.post("/schedule")
async def schedule_campaign(campaign: ScheduledCampaign):
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        
        # MySQL ON DUPLICATE KEY UPDATE syntax
        sql = """
            INSERT INTO scheduled_campaigns 
            (id, schedule_time, title, template, promo_data, customers, promo_products, promo_bundles)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            schedule_time = VALUES(schedule_time),
            title = VALUES(title),
            promo_data = VALUES(promo_data),
            customers = VALUES(customers)
        """
        cursor.execute(sql, (
            campaign.id, 
            campaign.schedule_time, 
            campaign.title, 
            campaign.template,
            json.dumps(campaign.promo_data),
            json.dumps(campaign.customers),
            json.dumps(campaign.promo_products),
            json.dumps(campaign.promo_bundles)
        ))
        conn.commit()
        cursor.close()
        conn.close()
        return {"status": "success", "message": f"Campaign {campaign.id} scheduled."}
    except Exception as e:
        logger.error(f"API Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/open/{cid}/{uid}")
def track_open(cid: str, uid: str, request: Request):
    log_event(cid, "open", request, customer_id=uid)
    pixel_data = b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
    return Response(content=pixel_data, media_type="image/gif")

@app.get("/click/{cid}/{uid}")
def track_click(cid: str, uid: str, url: str, request: Request):
    log_event(cid, "click", request, customer_id=uid, target_url=url)
    return RedirectResponse(url=url)

@app.get("/stats/{cid}")
def get_stats(cid: str):
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)
        
        # Get counts for opens and clicks
        # We use COUNT(*) for total events. 
        # If you want unique opens, use COUNT(DISTINCT customer_id)
        cursor.execute("""
            SELECT 
                SUM(CASE WHEN action = 'open' THEN 1 ELSE 0 END) as opens,
                SUM(CASE WHEN action = 'click' THEN 1 ELSE 0 END) as clicks
            FROM events 
            WHERE campaign_id = %s
        """, (cid,))
        
        res = cursor.fetchone()
        cursor.close()
        conn.close()
        
        return {
            "opens": res['opens'] or 0,
            "clicks": res['clicks'] or 0
        }
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        return {"opens": 0, "clicks": 0}

def log_event(cid, action, request, customer_id=None, target_url=None):
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO events (campaign_id, customer_id, action, ip, user_agent, target_url)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (cid, customer_id, action, request.client.host, request.headers.get("user-agent"), target_url))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging event: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
