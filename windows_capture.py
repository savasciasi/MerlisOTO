# windows_capture.py
import win32gui
import win32process
import psutil

def enum_hwnds_for_pid(pid: int):
    hwnds = []
    def callback(hwnd, extra):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _tid, _pid = win32process.GetWindowThreadProcessId(hwnd)
            if _pid == pid:
                # boş başlıkları ve ikonik (minimize) durumları geç
                if win32gui.GetWindowText(hwnd) and not win32gui.IsIconic(hwnd):
                    hwnds.append(hwnd)
        except Exception:
            pass
        return True
    win32gui.EnumWindows(callback, None)
    return hwnds

def get_window_rect_by_pid(pid: int):
    """PID’e ait görünen ilk ana pencerenin (left, top, right, bottom) rect’ini döndürür."""
    hwnds = enum_hwnds_for_pid(pid)
    if not hwnds:
        return None
    # En üstteki/ilk bulunanı al
    rect = win32gui.GetWindowRect(hwnds[0])
    # Bazı oyunlarda pencere kenarlığı harici client area lazım olabilir;
    # şimdilik tüm rect'i kullanıyoruz.
    return rect  # (L, T, R, B)

def find_pid_by_name(substr: str):
    """Process adında veya komut satırında substr geçen ilk PID'i bulur."""
    substr = substr.lower()
    for p in psutil.process_iter(['pid','name','cmdline']):
        try:
            name = (p.info.get('name') or '').lower()
            cmd  = ' '.join(p.info.get('cmdline') or []).lower()
            if substr in name or substr in cmd:
                return p.info['pid']
        except Exception:
            pass
    return None
