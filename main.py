
import logging
import os
import time
import threading
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

try:
    from twilio.rest import Client
except ImportError:
    Client = None

try:
    import pywhatkit
except ImportError:
    pywhatkit = None

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    webdriver = None

# --- Configuration ---
BASE_URL = "https://www.nseindia.com"
# Use the corporate announcements landing page for NSE. Update this if NSE changes URL structure.
ANNOUNCEMENTS_URL = urljoin(BASE_URL, "/companies-listing/corporate-filings-announcements")
LAST_ID_FILE = Path(__file__).with_name("last_order_award_id.txt")
CHECK_INTERVAL_SECONDS = 15 * 60
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

# Environment variables for Twilio WhatsApp alerts
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")
TWILIO_WHATSAPP_TO = os.getenv("TWILIO_WHATSAPP_TO")

# Environment variable for PyWhatKit WhatsApp alerts
PYWHATKIT_PHONE_NUMBER = os.getenv("PYWHATKIT_PHONE_NUMBER")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)



class Announcement:
    def __init__(self, announcement_id: str, title: str, url: str):
        self.id = announcement_id
        self.title = title
        self.url = url

    def __repr__(self) -> str:
        return f"Announcement(id={self.id!r}, title={self.title!r}, url={self.url!r})"


def self_ping():
    url = os.environ.get('RENDER_EXTERNAL_URL', 'https://smc-bot-kwlx.onrender.com/')
    print(f"Self-ping started → {url}")
    while True:
        try:
            requests.get(url, timeout=10)
            print("Ping OK")
        except:
            print("Ping fail")
        time.sleep(600)


def load_last_processed_id() -> Optional[str]:
    if not LAST_ID_FILE.exists():
        return None
    text = LAST_ID_FILE.read_text(encoding="utf-8").strip()
    return text or None


def save_last_processed_id(announcement_id: str) -> None:
    LAST_ID_FILE.write_text(str(announcement_id), encoding="utf-8")
    logging.info("Saved last processed announcement id: %s", announcement_id)


def build_full_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return urljoin(BASE_URL, href)


def extract_announcement_id(url: str) -> str:
    parsed = urlparse(url)
    candidate = Path(parsed.path).name
    if candidate:
        return candidate
    qs = parse_qs(parsed.query)
    for key in ["id", "announcementId", "url"]:
        if key in qs and qs[key]:
            return qs[key][0]
    return url


def parse_order_award_links(html: str) -> List[Announcement]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)
    order_awards: List[Announcement] = []

    for a in anchors:
        href = a["href"].strip()
        text = a.get_text(separator=" ", strip=True)
        normalized_text = text.lower()
        normalized_href = href.lower()

        is_pdf = ".pdf" in normalized_href
        has_order_award = "order award" in normalized_text or "order awards" in normalized_text
        if not is_pdf:
            continue
        if not has_order_award:
            if "order" not in normalized_text or "award" not in normalized_text:
                continue

        full_url = build_full_url(href)
        announcement_id = extract_announcement_id(full_url)
        order_awards.append(Announcement(announcement_id=announcement_id, title=text, url=full_url))

    if not order_awards:
        logging.debug("No order award PDF links found in the page HTML.")

    return order_awards


def get_chrome_driver():
    if not webdriver:
        return None
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")  # Run in headless mode
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"user-agent={USER_AGENT}")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def fetch_announcements_html(session: requests.Session) -> str:
    if webdriver:
        driver = get_chrome_driver()
        if driver:
            try:
                driver.get(ANNOUNCEMENTS_URL)
                # Wait for some element to load, e.g., table or announcements
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "a"))
                )
                html = driver.page_source
                driver.quit()
                return html
            except Exception as e:
                logging.warning("Selenium failed: %s, falling back to requests", e)
                driver.quit()
    
    # Fallback to requests
    response = session.get(ANNOUNCEMENTS_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def send_whatsapp_alert(announcement: Announcement) -> None:
    message_body = (
        f"New NSE Order Awards announcement detected:\n"
        f"Title: {announcement.title}\n"
        f"Link: {announcement.url}"
    )

    if Client and all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_FROM}",
            body=message_body,
            to=f"whatsapp:{TWILIO_WHATSAPP_TO}",
        )
        logging.info("WhatsApp alert sent via Twilio: SID=%s", message.sid)
    elif pywhatkit and PYWHATKIT_PHONE_NUMBER:
        try:
            pywhatkit.sendwhatmsg_instantly(PYWHATKIT_PHONE_NUMBER, message_body)
            logging.info("WhatsApp alert sent via PyWhatKit to %s", PYWHATKIT_PHONE_NUMBER)
        except Exception as e:
            logging.error("Failed to send WhatsApp alert via PyWhatKit: %s", e)
    else:
        logging.warning(
            "WhatsApp not configured. Install twilio or pywhatkit and set environment variables to enable automatic alerts."
        )
        logging.info("NEW ORDER AWARD: %s %s", announcement.title, announcement.url)


def initialize_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def main() -> None:
    logging.info("Starting NSE Order Awards monitor for %s", ANNOUNCEMENTS_URL)
    threading.Thread(target=self_ping, daemon=True).start()
    session = initialize_session()
    last_id = load_last_processed_id()

    while True:
        try:
            html = fetch_announcements_html(session)
            order_awards = parse_order_award_links(html)

            if not order_awards:
                logging.info("No matching Order Awards links found this cycle.")
            else:
                newest_id = order_awards[0].id
                if last_id is None:
                    logging.info("First run detected. Setting last processed announcement id without sending alerts.")
                    save_last_processed_id(newest_id)
                    last_id = newest_id
                elif last_id != newest_id:
                    new_announcements = []
                    for announcement in order_awards:
                        if announcement.id == last_id:
                            break
                        new_announcements.append(announcement)

                    if new_announcements:
                        logging.info("Found %d new Order Awards announcements.", len(new_announcements))
                        for announcement in reversed(new_announcements):
                            send_whatsapp_alert(announcement)
                        last_id = new_announcements[0].id
                        save_last_processed_id(last_id)
                    else:
                        logging.info("No new Order Awards announcements since last check.")
                else:
                    logging.info("No new announcements. Last processed id remains %s", last_id)

        except Exception as exc:
            logging.exception("Error while checking announcements: %s", exc)

        logging.info("Sleeping for %d seconds...", CHECK_INTERVAL_SECONDS)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
