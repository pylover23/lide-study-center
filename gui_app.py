import argparse
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from gui_workers import TkTaskRunner
from lib.gui_service import GuiService, TOKEN_EXPIRED_MESSAGE, is_token_expired_message


class LideApp:
    def __init__(self, root, service=None):
        self.root = root
        self.service = service or GuiService()
        self.runner = TkTaskRunner(root)
        self.rooms_data = []
        self.settings = {}
        self.date_options = []
        self.current_seats = []
        self.selected_room = None
        self.selected_date = None
        self.selected_seat = None
        self.root.title("立德研学中心预约助手")
        self.root.minsize(960, 680)
        self._configure_style()
        self.container = ttk.Frame(root, padding=18)
        self.container.pack(fill="both", expand=True)
        self.show_login()

    def _configure_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Status.TLabel", foreground="#2563eb")
        style.configure("Muted.TLabel", foreground="#667085")
        style.configure("Available.TButton", foreground="#166534", padding=6)
        style.configure("Unavailable.TButton", foreground="#777777", padding=6)
        style.configure("Selected.TButton", foreground="#1d4ed8", padding=6, relief="solid")
        style.configure("Primary.TButton", padding=(14, 7))

    def _clear(self):
        for child in self.container.winfo_children():
            child.destroy()

    def show_login(self):
        self._clear()
        panel = ttk.Frame(self.container, padding=30)
        panel.place(relx=0.5, rely=0.42, anchor="center")
        ttk.Label(panel, text="立德研学中心预约助手", style="Title.TLabel").grid(row=0, column=0, columnspan=2, pady=(0, 24))

        ttk.Label(panel, text="学工号").grid(row=1, column=0, sticky="e", padx=(0, 10), pady=8)
        self.username_var = tk.StringVar(value=self.service.saved_username())
        username_entry = ttk.Entry(panel, textvariable=self.username_var, width=34)
        username_entry.grid(row=1, column=1, sticky="ew", pady=8)

        ttk.Label(panel, text="密码").grid(row=2, column=0, sticky="e", padx=(0, 10), pady=8)
        password_value = self.service.cfg.get("password", "") if self.service.has_saved_password() else ""
        self.password_var = tk.StringVar(value=password_value)
        password_entry = ttk.Entry(panel, textvariable=self.password_var, show="*", width=34)
        password_entry.grid(row=2, column=1, sticky="ew", pady=8)

        self.remember_var = tk.BooleanVar(value=bool(self.service.cfg.get("rememberCredentials")))
        ttk.Checkbutton(panel, text="保存密码到本机配置", variable=self.remember_var).grid(row=3, column=1, sticky="w", pady=8)

        self.login_button = ttk.Button(panel, text="登录", style="Primary.TButton", command=self._login_clicked)
        self.login_button.grid(row=4, column=1, sticky="ew", pady=(16, 8))
        self.login_status_var = tk.StringVar(value="")
        ttk.Label(panel, textvariable=self.login_status_var, style="Status.TLabel").grid(row=5, column=0, columnspan=2)
        panel.columnconfigure(1, weight=1)
        username_entry.focus_set()

    def _login_clicked(self):
        self.login_button.state(["disabled"])
        self.login_status_var.set("正在登录，请稍候...")
        username = self.username_var.get()
        password = self.password_var.get()
        remember = self.remember_var.get()
        self.runner.run(
            "login",
            lambda: self.service.login(username, password, remember),
            self._login_success,
            self._login_error,
            lambda: self.login_button.state(["!disabled"]),
        )

    def _login_success(self, _token):
        self.show_main()
        self.refresh_all()

    def _login_error(self, message):
        self.login_status_var.set(message)

    def show_main(self):
        self._clear()
        header = ttk.Frame(self.container)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="立德研学中心预约助手", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="已登录", style="Status.TLabel").pack(side="left", padx=18)
        ttk.Button(header, text="刷新全部", command=self.refresh_all).pack(side="right")

        self.notebook = ttk.Notebook(self.container)
        self.notebook.pack(fill="both", expand=True)
        self.records_tab = ttk.Frame(self.notebook, padding=12)
        self.booking_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.records_tab, text="预约记录")
        self.notebook.add(self.booking_tab, text="预约选座")
        self._build_records_tab()
        self._build_booking_tab()

    def _build_records_tab(self):
        top = ttk.Frame(self.records_tab)
        top.pack(fill="x", pady=(0, 10))
        ttk.Button(top, text="刷新记录", command=self.refresh_records).pack(side="left")
        self.appointment_count_var = tk.StringVar(value="预约记录：0")
        self.breach_count_var = tk.StringVar(value="违约记录：0")
        ttk.Label(top, textvariable=self.appointment_count_var).pack(side="left", padx=18)
        ttk.Label(top, textvariable=self.breach_count_var).pack(side="left")

        frames = ttk.Frame(self.records_tab)
        frames.pack(fill="both", expand=True)
        appointment_frame = ttk.LabelFrame(frames, text="预约记录", padding=8)
        breach_frame = ttk.LabelFrame(frames, text="违约记录", padding=8)
        appointment_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
        breach_frame.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.appointment_empty = ttk.Label(appointment_frame, text="暂无预约记录", style="Muted.TLabel")
        self.breach_empty = ttk.Label(breach_frame, text="暂无违约记录", style="Muted.TLabel")
        self.appointment_tree = self._make_records_tree(appointment_frame)
        self.breach_tree = self._make_records_tree(breach_frame)

    def _make_records_tree(self, parent):
        columns = ("date", "room", "seat", "status", "breach")
        tree = ttk.Treeview(parent, columns=columns, show="headings", height=15)
        headings = {"date": "日期", "room": "房间", "seat": "座位", "status": "状态", "breach": "违约原因"}
        widths = {"date": 150, "room": 150, "seat": 70, "status": 70, "breach": 120}
        for key in columns:
            tree.heading(key, text=headings[key])
            tree.column(key, width=widths[key], anchor="center")
        tree.pack(fill="both", expand=True)
        return tree

    def _build_booking_tab(self):
        controls = ttk.Frame(self.booking_tab)
        controls.pack(fill="x", pady=(0, 10))
        ttk.Label(controls, text="房间").pack(side="left")
        self.room_var = tk.StringVar()
        self.room_combo = ttk.Combobox(controls, textvariable=self.room_var, width=32, state="readonly")
        self.room_combo.pack(side="left", padx=(8, 18))
        self.room_combo.bind("<<ComboboxSelected>>", self._room_changed)
        ttk.Label(controls, text="日期/时间").pack(side="left")
        self.date_var = tk.StringVar()
        self.date_combo = ttk.Combobox(controls, textvariable=self.date_var, width=24, state="readonly")
        self.date_combo.pack(side="left", padx=(8, 18))
        self.date_combo.bind("<<ComboboxSelected>>", self._date_changed)
        ttk.Button(controls, text="刷新房间", command=self.refresh_rooms).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="刷新座位", command=self.refresh_seats).pack(side="left")

        grid_frame = ttk.LabelFrame(self.booking_tab, text="座位", padding=8)
        grid_frame.pack(fill="both", expand=True)
        self.seat_canvas = tk.Canvas(grid_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(grid_frame, orient="vertical", command=self.seat_canvas.yview)
        self.seat_inner = ttk.Frame(self.seat_canvas)
        self.seat_window = self.seat_canvas.create_window((0, 0), window=self.seat_inner, anchor="nw")
        self.seat_canvas.configure(yscrollcommand=scrollbar.set)
        self.seat_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.seat_inner.bind("<Configure>", lambda _e: self.seat_canvas.configure(scrollregion=self.seat_canvas.bbox("all")))
        self.seat_canvas.bind("<Configure>", lambda e: self.seat_canvas.itemconfigure(self.seat_window, width=e.width))

        action = ttk.Frame(self.booking_tab)
        action.pack(fill="x", pady=(10, 0))
        self.selection_var = tk.StringVar(value="已选：未选择")
        ttk.Label(action, textvariable=self.selection_var).pack(side="left")
        self.reserve_button = ttk.Button(action, text="预约此座位", command=self.reserve_selected)
        self.reserve_button.pack(side="right")
        self.reserve_button.state(["disabled"])

        self.log_text = tk.Text(self.booking_tab, height=5, wrap="word", state="disabled")
        self.log_text.pack(fill="x", pady=(10, 0))

    def refresh_all(self):
        self.refresh_records()
        self.refresh_rooms()

    def refresh_records(self):
        self.runner.run("records", self.service.records, self._records_loaded, self._append_error)

    def _records_loaded(self, records):
        self.appointment_count_var.set(f"预约记录：{records['appointmentCount']}")
        self.breach_count_var.set(f"违约记录：{records['breachCount']}")
        self._fill_records_tree(self.appointment_tree, self.appointment_empty, records["appointments"], "暂无预约记录")
        self._fill_records_tree(self.breach_tree, self.breach_empty, records["breaches"], "暂无违约记录")

    def _fill_records_tree(self, tree, empty_label, rows, empty_text):
        for item in tree.get_children():
            tree.delete(item)
        if rows:
            empty_label.pack_forget()
            if not tree.winfo_ismapped():
                tree.pack(fill="both", expand=True)
        else:
            tree.pack_forget()
            empty_label.configure(text=empty_text)
            empty_label.pack(anchor="center", pady=30)
            return
        for row in rows:
            begin = row.get("begin") or ""
            end = row.get("end") or ""
            date_text = begin[:10] + " 至 " + end[:10]
            tree.insert("", "end", values=(
                date_text,
                row.get("room", ""),
                row.get("seat", ""),
                row.get("statusText", row.get("status", "")),
                row.get("breachReason", ""),
            ))

    def refresh_rooms(self):
        self._append_log("正在刷新房间...")
        self.runner.run("rooms", self.service.rooms, self._rooms_loaded, self._append_error)
        self.runner.run("settings", self.service.system_settings, self._settings_loaded, self._append_error)

    def _rooms_loaded(self, rooms):
        self.rooms_data = rooms
        values = [room["name"] or room["roomId"] for room in rooms]
        self.room_combo.configure(values=values)
        if values and not self.room_var.get():
            self.room_combo.current(0)
            self._room_changed()
        self._append_log(f"已加载房间：{len(rooms)}")

    def _settings_loaded(self, settings):
        self.settings = settings
        self._refresh_date_options()

    def _room_changed(self, _event=None):
        index = self.room_combo.current()
        self.selected_room = self.rooms_data[index] if 0 <= index < len(self.rooms_data) else None
        self.selected_seat = None
        self._refresh_date_options()
        self._update_selection_label()

    def _refresh_date_options(self):
        if not self.selected_room:
            return
        self.date_options = self.service.available_date_options(self.selected_room, self.settings or {})
        labels = [item["label"] for item in self.date_options]
        self.date_combo.configure(values=labels)
        if labels:
            self.date_combo.current(0)
            self._date_changed()

    def _date_changed(self, _event=None):
        index = self.date_combo.current()
        self.selected_date = self.date_options[index]["date"] if 0 <= index < len(self.date_options) else None
        self.selected_seat = None
        self._update_selection_label()

    def refresh_seats(self):
        if not self.selected_room or not self.selected_date:
            self._append_log("请先选择房间和日期")
            return
        room_id = self.selected_room["roomId"]
        date_str = self.selected_date
        self._append_log("正在刷新座位...")
        self.runner.run(
            "seats",
            lambda: self.service.seats(room_id, date_str),
            self._seats_loaded,
            self._append_error,
        )

    def _seats_loaded(self, seats):
        self.current_seats = seats
        self.selected_seat = None
        for child in self.seat_inner.winfo_children():
            child.destroy()
        for idx, seat in enumerate(seats):
            available = seat["available"]
            style = "Available.TButton" if available else "Unavailable.TButton"
            button = ttk.Button(
                self.seat_inner,
                text=f"{seat['no']}号",
                width=8,
                style=style,
                command=lambda item=seat: self._select_seat(item),
            )
            button.grid(row=idx // 10, column=idx % 10, padx=4, pady=4, sticky="ew")
            if not available:
                button.state(["disabled"])
            seat["button"] = button
        for column in range(10):
            self.seat_inner.columnconfigure(column, weight=1)
        self._append_log(f"已加载座位：{len(seats)}，可约：{sum(1 for x in seats if x['available'])}")
        self._update_selection_label()

    def _select_seat(self, seat):
        if not seat.get("available"):
            return
        if self.selected_seat and self.selected_seat.get("button"):
            self.selected_seat["button"].configure(style="Available.TButton")
        self.selected_seat = seat
        seat["button"].configure(style="Selected.TButton")
        self._update_selection_label()

    def _update_selection_label(self):
        room_name = self.selected_room["name"] if self.selected_room else "未选房间"
        date_text = self.selected_date or "未选日期"
        seat_text = f"{self.selected_seat['no']}号" if self.selected_seat else "未选座位"
        self.selection_var.set(f"已选：{room_name} {date_text} {seat_text}")
        if self.selected_room and self.selected_date and self.selected_seat:
            self.reserve_button.state(["!disabled"])
        else:
            self.reserve_button.state(["disabled"])

    def reserve_selected(self):
        if not (self.selected_room and self.selected_date and self.selected_seat):
            return
        self.reserve_button.state(["disabled"])
        room_id = self.selected_room["roomId"]
        seat_no = int(self.selected_seat["no"])
        date_str = self.selected_date

        def progress(message):
            self.root.after(0, lambda msg=message: self._append_log(msg))

        self.runner.run(
            "reserve",
            lambda: self.service.reserve(room_id, seat_no, date_str, progress=progress),
            self._reserve_finished,
            self._reserve_error,
            self._update_selection_label,
        )

    def _reserve_finished(self, result):
        self._append_log(result["message"])
        if result["ok"]:
            messagebox.showinfo("预约成功", "预约成功")
            self.refresh_records()
            self.refresh_seats()
        else:
            messagebox.showerror("预约失败", result["message"])

    def _handle_token_expired(self, message):
        if not is_token_expired_message(message):
            return False
        if hasattr(self.service, "token"):
            self.service.token = None
        self._append_log(TOKEN_EXPIRED_MESSAGE)
        messagebox.showwarning("登录已失效", TOKEN_EXPIRED_MESSAGE)
        self.show_login()
        return True

    def _reserve_error(self, message):
        if self._handle_token_expired(message):
            return
        self._append_log(message)
        messagebox.showerror("预约失败", message)

    def _append_error(self, message):
        if self._handle_token_expired(message):
            return
        self._append_log(message)
        messagebox.showerror("操作失败", message)

    def _append_log(self, message):
        if not hasattr(self, "log_text"):
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", str(message) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args(argv)
    root = tk.Tk()
    if args.smoke:
        root.withdraw()
        LideApp(root)
        print("GUI_SMOKE_OK")
        root.destroy()
        return 0
    LideApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
