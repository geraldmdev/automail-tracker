from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import RedirectResponse
import sqlite3
import datetime
import os

app = FastAPI(title="AutoMail Tracking Service")

# Database initialization
DB_PATH = "tracking.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT,
            customer_id TEXT,
            action TEXT,
            timestamp DATETIME,
            ip TEXT,
            user_agent TEXT,
            target_url TEXT
        )
    ''')
    
    # Simple migration: try to add customer_id if table exists but column doesn't
    try:
        cursor.execute("ALTER TABLE events ADD COLUMN customer_id TEXT")
    except sqlite3.OperationalError:
        # Column already exists
        pass
        
    conn.commit()
    conn.close()

init_db()

@app.get("/")
def read_root():
    return {"status": "operational", "service": "AutoMail Tracking"}

@app.get("/open/{cid}")
@app.get("/open/{cid}/{uid}")
def track_open(cid: str, request: Request, uid: str = None):
    """
    Tracking pixel endpoint.
    Example: GET /open/123 or GET /open/123/user456
    """
    log_event(cid, "open", request, customer_id=uid)
    
    # Return 1x1 transparent GIF
    pixel_data = b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
    return Response(content=pixel_data, media_type="image/gif", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })

@app.get("/click/{cid}")
@app.get("/click/{cid}/{uid}")
def track_click(cid: str, url: str, request: Request, uid: str = None):
    """
    Link redirect endpoint.
    Example: GET /click/123?url=... or GET /click/123/user456?url=...
    """
    log_event(cid, "click", request, customer_id=uid, target_url=url)
    return RedirectResponse(url=url)

@app.get("/stats/{cid}")
def get_stats(cid: str):
    """
    Fetch stats for a specific campaign.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(DISTINCT customer_id) FROM events WHERE campaign_id = ? AND action = 'open'", (cid,))
    unique_opens = cursor.fetchone()[0]
    
    # Also count total opens for internal stats if needed, but return unique to user
    cursor.execute("SELECT COUNT(*) FROM events WHERE campaign_id = ? AND action = 'open'", (cid,))
    total_opens = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(DISTINCT customer_id) FROM events WHERE campaign_id = ? AND action = 'click'", (cid,))
    unique_clicks = cursor.fetchone()[0]
    
    conn.close()
    return {
        "campaign_id": cid,
        "opens": unique_opens,
        "clicks": unique_clicks,
        "total_opens": total_opens
    }

def log_event(cid, action, request, customer_id=None, target_url=None):
    try:
        ip = request.client.host
        ua = request.headers.get("user-agent", "unknown")
        ts = datetime.datetime.now().isoformat()
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO events (campaign_id, customer_id, action, timestamp, ip, user_agent, target_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (cid, customer_id, action, ts, ip, ua, target_url))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error logging event: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
