import pyautogui
import time
import random
import datetime as dt
import sys

try:
    import holidays  # pip install holidays
except ImportError:
    holidays = None

def is_working_day_today() -> bool:
    today = dt.date.today()
    # 0=Po ... 6=Ne
    if today.weekday() >= 5:
        print(f"Dnes {today} je víkend. Skript sa nespustí.")
        return False
    if holidays is not None:
        sk_holidays = holidays.CountryHoliday("SK")
        if today in sk_holidays:
            print(f"Dnes {today} je sviatok ({sk_holidays.get(today)}). Skript sa nespustí.")
            return False
    else:
        static_holidays = {
            "01-01","05-01","05-08","07-05","08-29","09-01","09-15",
            "11-01","11-17","12-24","12-25","12-26"
        }
        if today.strftime("%m-%d") in static_holidays:
            print(f"Dnes {today} je sviatok (zo statického zoznamu). Skript sa nespustí.")
            return False
    print(f"Dnes {today} je pracovný deň. Skript beží ďalej...")
    return True

def get_ascii_art():
    local_ascii = [
        "¯\\_(ツ)_/¯", "(ง'̀-'́)ง", "ʕ•ᴥ•ʔ", "(¬‿¬)", "(•_•)", "(╯°□°）╯︵ ┻━┻",
        "(ノಠ益ಠ)ノ彡┻━┻", "(☞ﾟヮﾟ)☞", "^_^", "(ಠ_ಠ)", "(づ｡◕‿‿◕｡)づ", "(≧◡≦)"
    ]
    return random.choice(local_ascii)

def mouse_jiggler(interval=30):
    print(f"Jiggler started. Jiggle every {interval} seconds. Press Ctrl+C to stop.")
    try:
        while True:
            for remaining in range(interval, 0, -1):
                print(f"Next jiggle in {remaining} seconds... {get_ascii_art()}")
                time.sleep(1)

            x, y = pyautogui.position()
            pyautogui.moveTo(x + 1, y + 1, duration=0.1)
            pyautogui.moveTo(x, y, duration=0.1)
            pyautogui.press('shift')

            print("Jiggling...")
    except KeyboardInterrupt:
        print("Stopped.")
        input("Press Enter to exit...")

if __name__ == "__main__":
    if not is_working_day_today():
        sys.exit(0)  # víkend alebo sviatok – len vypíše a skončí
    mouse_jiggler(interval=30)
