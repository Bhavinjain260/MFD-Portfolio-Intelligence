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

        # Wait for dropdown
        dropdown = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "ddlScheme"))
        )

        select = Select(dropdown)
        select.select_by_visible_text("Scheme Code Master Physical")

        time.sleep(1)  # let postback settle if any

        export_btn = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.ID, "btnText"))
        )
        export_btn.click()

        time.sleep(5)  # allow download to complete
        print(f"Export triggered. Check: {DOWNLOAD_DIR}")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()