class LoginSelectors:
    USERNAME_INPUT = "input[name='i']"
    PASSWORD_INPUT = "input[name='p']"
    LOGIN_BUTTON = "button[type='submit'], button:has-text('Login'), button"

class CalendarSelectors:
    VIEW_CLASSES_BUTTON = "text=/View Classes/i"
    EVENT_BOX = "div.fc-event"
    EVENT_CLICKABLE = "div.fc-event a, div.fc-event div"
    COUNTDOWN_TEXT = "//*[contains(text(),'join') or contains(text(),'h')]"
    JOIN_BUTTON = "a.joinBtn, button:has-text('Join'), text='Join'"

class MeetingSelectors:
    IFRAME = "iframe"
    MICROPHONE_BUTTON = "button[aria-label='Microphone']"
    ECHO_YES_BUTTON = "button[aria-label='Echo is audible'], button:has-text('Yes')"
    CLOSE_MODAL_BUTTON = "button[aria-label='Close']"
