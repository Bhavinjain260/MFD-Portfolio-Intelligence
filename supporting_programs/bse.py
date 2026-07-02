
"""
Downloads BSE StAR MF Scheme Master (Physical) report.
Requires: pip install selenium webdriver-manager
"""

import os
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

URL = "https://www.bsestarmf.in/RptSchemeMaster.aspx"
DOWNLOAD_DIR = os.path.abspath("./downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def main():
    options = webdriver.ChromeOptions()
    prefs = {
        "download.default_directory": DOWNLOAD_DIR,
        "download.prompt_for_download": False,
    }
    options.add_experimental_option("prefs", prefs)
    # options.add_argument("--headless=new")  # uncomment to run headless

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=options
    )

    try:
        driver.get(URL)
        time.sleep(3)

        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.ID, "ddlTypeOption"))
            )
        except Exception:
            frames = driver.find_elements(By.TAG_NAME, "iframe")
            found = False
            for f in frames:
                driver.switch_to.default_content()
                driver.switch_to.frame(f)
                if driver.find_elements(By.ID, "ddlTypeOption"):
                    found = True
                    break
            if not found:
                driver.switch_to.default_content()
                with open("page_debug.html", "w", encoding="utf-8") as fh:
                    fh.write(driver.page_source)
                driver.save_screenshot("page_debug.png")
                raise RuntimeError(
                    "ddlTypeOption not found. Dumped page_debug.html / page_debug.png"
                )

        dropdown = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "ddlTypeOption"))
        )

        select = Select(dropdown)
        select.select_by_value("SCHEMEMASTERPHYSICAL")

        time.sleep(1)  # let postback settle if any

        before = set(os.listdir(DOWNLOAD_DIR))

        export_btn = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.ID, "btnText"))
        )
        export_btn.click()

        timeout = 350
        waited = 0
        new_file = None

        while True:
            time.sleep(1)
            waited += 1
            current = set(os.listdir(DOWNLOAD_DIR))
            print(waited)
            added = current - before

            if waited >= timeout:
                if any(f.endswith(".crdownload") for f in added):
                    continue  # still downloading, keep looping
                txt_files = [f for f in added if f.endswith(".txt")]
                if txt_files:
                    new_file = txt_files[0]
                    break
                else:
                    break  # exit, no crdownload, no txt

        if new_file:
            print(f"Export complete: {os.path.join(DOWNLOAD_DIR, new_file)}")
        else:
            print(f"Download did not finish. Check: {DOWNLOAD_DIR}")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()

