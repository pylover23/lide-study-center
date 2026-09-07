import queue
import threading
from tkinter import messagebox


class TkTaskRunner:

    def __init__(self, root):
        self.root = root

    def run(self, label: str, func, on_success, on_error=None, on_finally=None) -> None:
        if on_error is None:
            on_error = lambda exc_message: messagebox.showerror("操作失败", exc_message)

        results = queue.Queue()

        def worker():
            try:
                result = func()
                results.put(("ok", label, result))
            except Exception as exc:
                results.put(("error", label, str(exc)))

        def poll():
            try:
                status, _label, payload = results.get_nowait()
            except queue.Empty:
                self.root.after(50, poll)
                return
            try:
                if status == "ok":
                    on_success(payload)
                else:
                    on_error(payload)
            finally:
                if on_finally is not None:
                    on_finally()

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(50, poll)
