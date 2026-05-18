print("🚀 Script started")

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from datetime import datetime
import time
import re

print("🚀 Starting browser...")

# ================= CHROME OPTIONS =================
chrome_options = Options()
chrome_options.binary_location = r"C:\Users\Ravi\aut\chrome-win64\chrome.exe"

chrome_options.add_argument("--use-fake-ui-for-media-stream")

chrome_options.add_experimental_option("prefs", {
    "profile.default_content_setting_values.media_stream_mic": 1,
    "profile.default_content_setting_values.media_stream_camera": 1
})

# ================= DRIVER =================
driver = webdriver.Chrome(options=chrome_options)

driver.maximize_window()
driver.get("https://myclass.lpu.in/")

wait = WebDriverWait(driver, 25)

# ================= LOGIN =================
print("🔐 Waiting for login page...")

time.sleep(2)

wait.until(EC.presence_of_element_located(
    (By.CSS_SELECTOR, "input[name='i']")
)).send_keys("12316665")

driver.find_element(By.NAME, "p").send_keys("Cool@135")
driver.find_element(By.XPATH, "//button").click()

print("✅ Logged in")

# ================= OPEN CLASSES =================
wait.until(EC.element_to_be_clickable(
    (By.XPATH, "//*[contains(text(),'View Classes')]"))
).click()

print("📅 Calendar opened")

# ================= CLICK EVENT =================
time.sleep(5)

events = wait.until(EC.presence_of_all_elements_located(
    (By.XPATH, "//div[contains(@class,'fc-event')]")
))

for event in events:
    try:
        clickable = event.find_element(By.XPATH, ".//a | .//div")
        driver.execute_script("arguments[0].click();", clickable)
        print("📌 Event opened")
        break
    except:
        continue

time.sleep(2)

# ================= HANDLE COUNTDOWN =================
print("🔍 Checking countdown...")

try:
    countdown_text = driver.find_element(
        By.XPATH, "//*[contains(text(),'join')]"
    ).text.strip()

    print("🕒 Countdown:", countdown_text)

    if countdown_text:
        h = re.search(r'(\d+)h', countdown_text)
        m = re.search(r'(\d+)m', countdown_text)
        s = re.search(r'(\d+)s', countdown_text)

        hours = int(h.group(1)) if h else 0
        minutes = int(m.group(1)) if m else 0
        seconds = int(s.group(1)) if s else 0

        total_seconds = hours*3600 + minutes*60 + seconds

        if total_seconds > 0:
            print(f"⏳ Waiting {total_seconds} seconds...")
            time.sleep(total_seconds + 5)

except:
    print("⚡ Countdown not found")

# ================= CLICK JOIN =================
print("🚀 Waiting for Join button...")

while True:
    try:
        join_btn = driver.find_element(
            By.XPATH,
            "//a[contains(@class,'joinBtn')] | //*[contains(text(),'Join')]"
        )

        if join_btn.is_displayed():
            print("🚀 Join button found!")
            driver.execute_script("arguments[0].click();", join_btn)
            break

    except:
        pass

    time.sleep(5)

# ================= SWITCH TO IFRAME (FIXED) =================
print("🔄 Waiting for iframe...")

iframe_found = False

for _ in range(20):  # try up to ~100 seconds
    try:
        driver.switch_to.default_content()

        iframes = driver.find_elements(By.TAG_NAME, "iframe")

        if len(iframes) > 0:
            driver.switch_to.frame(iframes[0])
            print("✅ Switched to iframe")
            iframe_found = True
            break

    except:
        pass

    print("⏳ Iframe not loaded yet...")
    time.sleep(5)

if not iframe_found:
    raise Exception("❌ Iframe not found after waiting")

# ================= CLICK MICROPHONE =================
print("🎤 Clicking microphone...")

mic_btn = wait.until(EC.element_to_be_clickable(
    (By.XPATH, "//button[@aria-label='Microphone']")
))

driver.execute_script("arguments[0].click();", mic_btn)

print("🎤 Mic selected")

# ================= CLICK YES =================
print("👍 Clicking YES...")

print("👍 Waiting for YES button...")

yes_found = False

for _ in range(20):  # wait up to ~100 sec
    try:
        yes_btns = driver.find_elements(By.XPATH,
            "//button[@aria-label='Echo is audible'] | //span[text()='Yes']/ancestor::button"
        )

        if len(yes_btns) > 0:
            driver.execute_script("arguments[0].click();", yes_btns[0])
            print("👍 YES clicked")
            yes_found = True
            break

    except:
        pass

    print("⏳ YES not visible yet...")
    time.sleep(5)

if not yes_found:
    print("⚠️ YES button not found — maybe auto-joined")

print("🎉 Fully joined class successfully!")

# ================= GET END TIME =================
print("⏳ Keeping session alive... (Press CTRL+C to stop)")

while True:
    time.sleep(60)

print("🕒 Timing:", timing_text)

times = re.findall(r'(\d{2}:\d{2})', timing_text)
end_time_str = times[-1]

end_hour, end_minute = map(int, end_time_str.split(":"))

print(f"⏱ End time: {end_hour}:{end_minute}")

# ================= KEEP SESSION ALIVE =================
print("⏳ Keeping session alive...")

while True:
    now = datetime.now()

    if now.hour > end_hour or (now.hour == end_hour and now.minute >= end_minute):
        print("🏁 Class ended. Closing browser.")
        break

    time.sleep(30)

driver.quit()