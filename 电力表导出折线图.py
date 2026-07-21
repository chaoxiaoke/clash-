import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import pyqtgraph as pg

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QPushButton, QListWidget,
    QFileDialog, QInputDialog, QMessageBox, QLabel, QHBoxLayout,
    QVBoxLayout, QToolTip
)
from PyQt6.QtGui import QCursor, QDesktopServices
from PyQt6.QtCore import QUrl


def get_app_dir():
    # 打包成 exe 后，sys.executable 就是 exe 的真实路径
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent

    # 直接运行 py 文件时，使用 py 文件所在目录
    return Path(__file__).parent


APP_DIR = get_app_dir()
DATA_DIR = APP_DIR / "电力表历史记录"
DATA_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_FILE = DATA_DIR / "history.json"


def load_history():
    if not HISTORY_FILE.exists():
        return {"last": None, "records": {}}

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "last" not in data:
        data["last"] = None

    if "records" not in data:
        data["records"] = {}

    return data


def save_history(history):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    temp_file = HISTORY_FILE.with_suffix(".json.tmp")

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp_file, HISTORY_FILE)

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        json.load(f)


def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name.strip() or "未命名记录"


def time_to_seconds(value):
    text = str(value)

    if " " in text:
        text = text.split(" ")[-1]

    text = text.split(".")[0]
    h, m, s = text.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def seconds_to_time(seconds):
    seconds = int(seconds) % 86400
    h = seconds // 3600
    m = seconds % 3600 // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def fix_midnight_wrap(seconds_array):
    fixed = seconds_array.astype(float).copy()

    for i in range(1, len(fixed)):
        if fixed[i] < fixed[i - 1]:
            fixed[i:] += 86400

    return fixed


def calculate_energy_kwh(x_seconds, power_w):
    if len(x_seconds) < 2:
        return np.zeros(len(x_seconds)), 0.0

    dt = np.diff(x_seconds)
    avg_power = (power_w[:-1] + power_w[1:]) / 2
    energy_parts = avg_power * dt / 3600000

    cumulative_energy = np.zeros(len(x_seconds))
    cumulative_energy[1:] = np.cumsum(energy_parts)

    return cumulative_energy, cumulative_energy[-1]


class TimeAxisItem(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [seconds_to_time(v) for v in values]


def read_umeter_txt(file_path):
    try:
        df = pd.read_csv(
            file_path,
            sep=r"\s+",
            skiprows=1,
            names=["time_text", "voltage_v", "current_a", "d_plus_v", "d_minus_v"],
            encoding="ANSI"
        )
    except UnicodeDecodeError:
        df = pd.read_csv(
            file_path,
            sep=r"\s+",
            skiprows=1,
            names=["time_text", "voltage_v", "current_a", "d_plus_v", "d_minus_v"],
            encoding="gb18030"
        )

    df["time_text"] = df["time_text"].astype(str)
    seconds = df["time_text"].apply(time_to_seconds).to_numpy()
    df["time_seconds"] = fix_midnight_wrap(seconds)

    df["voltage_v"] = pd.to_numeric(df["voltage_v"])
    df["current_a"] = pd.to_numeric(df["current_a"])
    df["power_w"] = df["voltage_v"] * df["current_a"]

    return df


def prepare_df(df):
    df["time_text"] = df["time_text"].astype(str)

    if "time_seconds" not in df.columns:
        seconds = df["time_text"].apply(time_to_seconds).to_numpy()
        df["time_seconds"] = fix_midnight_wrap(seconds)

    df["voltage_v"] = pd.to_numeric(df["voltage_v"])
    df["current_a"] = pd.to_numeric(df["current_a"])

    if "power_w" not in df.columns:
        df["power_w"] = df["voltage_v"] * df["current_a"]

    return df


class PowerViewer(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("电力表数据查看器")
        self.resize(1250, 850)

        pg.setConfigOptions(antialias=False)

        self.df = None
        self.record_name = None
        self.hover_proxy = None

        self.main_widget = QWidget()
        self.setCentralWidget(self.main_widget)

        self.root_layout = QVBoxLayout(self.main_widget)

        self.title_label = QLabel("电力表数据查看器")
        self.title_label.setStyleSheet("font-size: 20px; font-weight: bold;")
        self.root_layout.addWidget(self.title_label)

        self.button_layout = QHBoxLayout()

        self.import_button = QPushButton("导入新的 TXT 文件")
        self.last_button = QPushButton("打开上次记录")
        self.open_button = QPushButton("打开选中历史记录")
        self.rename_button = QPushButton("重命名选中记录")
        self.folder_button = QPushButton("打开保存目录")

        self.button_layout.addWidget(self.import_button)
        self.button_layout.addWidget(self.last_button)
        self.button_layout.addWidget(self.open_button)
        self.button_layout.addWidget(self.rename_button)
        self.button_layout.addWidget(self.folder_button)
        self.button_layout.addStretch()

        self.root_layout.addLayout(self.button_layout)

        self.path_label = QLabel(f"保存目录：{DATA_DIR}")
        self.path_label.setStyleSheet(
            "background-color: #eeeeee; padding: 6px; font-size: 12px;"
        )
        self.root_layout.addWidget(self.path_label)

        self.info_label = QLabel("把鼠标放到折线图上查看数据")
        self.info_label.setStyleSheet(
            "background-color: #fff7cc; padding: 8px; font-size: 14px;"
        )
        self.root_layout.addWidget(self.info_label)

        self.energy_label = QLabel("总耗电量：暂无数据")
        self.energy_label.setStyleSheet(
            "background-color: #e8f4ff; padding: 8px; font-size: 15px; font-weight: bold;"
        )
        self.root_layout.addWidget(self.energy_label)

        self.content_layout = QHBoxLayout()
        self.root_layout.addLayout(self.content_layout)

        self.left_layout = QVBoxLayout()
        self.history_title = QLabel("历史记录：")
        self.history_list = QListWidget()

        self.left_layout.addWidget(self.history_title)
        self.left_layout.addWidget(self.history_list)

        self.content_layout.addLayout(self.left_layout, 1)

        self.plot_widget = pg.GraphicsLayoutWidget()
        self.content_layout.addWidget(self.plot_widget, 5)

        self.import_button.clicked.connect(self.import_new_file)
        self.last_button.clicked.connect(self.open_last_record)
        self.open_button.clicked.connect(self.open_selected_record)
        self.rename_button.clicked.connect(self.rename_selected_record)
        self.folder_button.clicked.connect(self.open_history_folder)
        self.history_list.itemDoubleClicked.connect(self.open_selected_record)

        self.refresh_list()

    def refresh_list(self):
        self.history_list.clear()

        try:
            history = load_history()
        except Exception as e:
            QMessageBox.critical(
                self,
                "读取历史失败",
                f"读取 history.json 失败：\n{e}\n\n路径：\n{HISTORY_FILE}"
            )
            return

        for name in history["records"]:
            self.history_list.addItem(name)

        self.path_label.setText(
            f"保存目录：{DATA_DIR}    历史记录数量：{len(history['records'])}"
        )

    def open_history_folder(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(DATA_DIR)))

    def import_new_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "请选择电力表导出的 txt 文件",
            "",
            "Text files (*.txt);;All files (*.*)"
        )

        if not file_path:
            return

        default_name = Path(file_path).stem + "_" + datetime.now().strftime("%Y%m%d_%H%M%S")

        record_name, ok = QInputDialog.getText(
            self,
            "记录命名",
            "请输入这次数据的名字：",
            text=default_name
        )

        if not ok or not record_name:
            return

        try:
            history = load_history()
        except Exception:
            history = {"last": None, "records": {}}

        if record_name in history["records"]:
            result = QMessageBox.question(
                self,
                "覆盖确认",
                f"已经有记录：{record_name}\n是否覆盖？"
            )
            if result != QMessageBox.StandardButton.Yes:
                return

        try:
            df = read_umeter_txt(file_path)

            csv_path = DATA_DIR / f"{safe_filename(record_name)}.csv"
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")

            if not csv_path.exists():
                raise RuntimeError(f"CSV 文件没有生成：{csv_path}")

            history["records"][record_name] = {
                "name": record_name,
                "source_file": file_path,
                "csv_file": str(csv_path)
            }
            history["last"] = record_name

            save_history(history)

            test_history = load_history()
            if record_name not in test_history["records"]:
                raise RuntimeError("history.json 已写入，但重新读取后没有找到刚保存的记录。")

            self.refresh_list()
            self.draw_chart(df, record_name)

            QMessageBox.information(
                self,
                "保存成功",
                f"已经保存历史记录。\n\nJSON：\n{HISTORY_FILE}\n\nCSV：\n{csv_path}"
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "保存失败",
                f"导入或保存失败：\n{e}\n\n保存目录：\n{DATA_DIR}"
            )

    def open_last_record(self):
        try:
            history = load_history()
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e))
            return

        last_name = history.get("last")

        if not last_name or last_name not in history["records"]:
            QMessageBox.information(self, "提示", "还没有上次记录，请先导入一个 txt 文件。")
            return

        self.open_record_by_name(last_name)

    def open_selected_record(self, item=None):
        current_item = self.history_list.currentItem()

        if current_item is None:
            QMessageBox.information(self, "提示", "请先选择一个历史记录。")
            return

        self.open_record_by_name(current_item.text())

    def open_record_by_name(self, record_name):
        try:
            history = load_history()
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e))
            return

        if record_name not in history["records"]:
            QMessageBox.warning(self, "错误", "找不到这个历史记录。")
            return

        record = history["records"][record_name]
        csv_file = Path(record["csv_file"])

        if not csv_file.exists():
            QMessageBox.warning(self, "错误", f"找不到历史数据文件：\n{csv_file}")
            return

        df = pd.read_csv(csv_file, encoding="utf-8-sig")

        history["last"] = record_name
        save_history(history)

        self.draw_chart(df, record_name)

    def rename_selected_record(self):
        item = self.history_list.currentItem()

        if item is None:
            QMessageBox.information(self, "提示", "请先选择一个历史记录。")
            return

        old_name = item.text()

        new_name, ok = QInputDialog.getText(
            self,
            "重命名",
            "请输入新的记录名字：",
            text=old_name
        )

        if not ok or not new_name or new_name == old_name:
            return

        try:
            history = load_history()
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e))
            return

        if new_name in history["records"]:
            QMessageBox.warning(self, "错误", "这个名字已经存在。")
            return

        history["records"][new_name] = history["records"].pop(old_name)
        history["records"][new_name]["name"] = new_name

        if history.get("last") == old_name:
            history["last"] = new_name

        save_history(history)
        self.refresh_list()

    def add_max_min_marker(self, plot, x, y, unit, color):
        max_index = int(y.argmax())
        min_index = int(y.argmin())

        max_x = x[max_index]
        max_y = y[max_index]
        min_x = x[min_index]
        min_y = y[min_index]

        max_point = pg.ScatterPlotItem(
            [max_x],
            [max_y],
            size=12,
            brush=pg.mkBrush(color),
            pen=pg.mkPen("black", width=1)
        )
        min_point = pg.ScatterPlotItem(
            [min_x],
            [min_y],
            size=12,
            brush=pg.mkBrush(color),
            pen=pg.mkPen("black", width=1)
        )

        plot.addItem(max_point)
        plot.addItem(min_point)

        max_text = pg.TextItem(
            f"最高 {max_y:.3f}{unit}\n{seconds_to_time(max_x)}",
            color=color,
            anchor=(0, 1)
        )
        min_text = pg.TextItem(
            f"最低 {min_y:.3f}{unit}\n{seconds_to_time(min_x)}",
            color=color,
            anchor=(0, 0)
        )

        max_text.setPos(max_x, max_y)
        min_text.setPos(min_x, min_y)

        plot.addItem(max_text)
        plot.addItem(min_text)

    def draw_chart(self, df, record_name):
        self.df = prepare_df(df)
        self.record_name = record_name

        self.plot_widget.clear()

        x = self.df["time_seconds"].to_numpy()
        time_text = self.df["time_text"].to_numpy()
        power = self.df["power_w"].to_numpy()
        voltage = self.df["voltage_v"].to_numpy()
        current = self.df["current_a"].to_numpy()

        cumulative_energy, total_energy = calculate_energy_kwh(x, power)

        self.info_label.setText("把鼠标放到折线图上查看数据")
        self.energy_label.setText(f"本次总耗电量：{total_energy:.6f} 度")

        axis1 = TimeAxisItem(orientation="bottom")
        axis2 = TimeAxisItem(orientation="bottom")
        axis3 = TimeAxisItem(orientation="bottom")

        power_plot = self.plot_widget.addPlot(
            row=0,
            col=0,
            axisItems={"bottom": axis1},
            title=f"总功耗(W) - {record_name}"
        )
        voltage_plot = self.plot_widget.addPlot(
            row=1,
            col=0,
            axisItems={"bottom": axis2},
            title="电压(V)"
        )
        current_plot = self.plot_widget.addPlot(
            row=2,
            col=0,
            axisItems={"bottom": axis3},
            title="电流(A)"
        )

        plots = [power_plot, voltage_plot, current_plot]

        for plot in plots:
            plot.showGrid(x=True, y=True, alpha=0.35)
            plot.setMouseEnabled(x=True, y=True)

        voltage_plot.setXLink(power_plot)
        current_plot.setXLink(power_plot)

        power_plot.plot(x, power, pen=pg.mkPen("red", width=2))
        voltage_plot.plot(x, voltage, pen=pg.mkPen("blue", width=2))
        current_plot.plot(x, current, pen=pg.mkPen("green", width=2))

        power_plot.setLabel("left", "W")
        voltage_plot.setLabel("left", "V")
        current_plot.setLabel("left", "A")
        current_plot.setLabel("bottom", "时间")

        self.add_max_min_marker(power_plot, x, power, "W", "red")
        self.add_max_min_marker(voltage_plot, x, voltage, "V", "blue")
        self.add_max_min_marker(current_plot, x, current, "A", "green")

        highlight_power = pg.ScatterPlotItem(size=12, brush=pg.mkBrush("darkred"))
        highlight_voltage = pg.ScatterPlotItem(size=12, brush=pg.mkBrush("darkblue"))
        highlight_current = pg.ScatterPlotItem(size=12, brush=pg.mkBrush("darkgreen"))

        power_plot.addItem(highlight_power)
        voltage_plot.addItem(highlight_voltage)
        current_plot.addItem(highlight_current)

        vertical_lines = []

        for plot in plots:
            line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#666666", width=1))
            plot.addItem(line)
            vertical_lines.append(line)

        last_index = {"value": None}

        def update_hover(pos):
            mouse_point = power_plot.vb.mapSceneToView(pos)
            mouse_x = mouse_point.x()

            if len(x) == 0:
                return

            index = int(abs(x - mouse_x).argmin())

            if last_index["value"] == index:
                return

            last_index["value"] = index

            highlight_power.setData([x[index]], [power[index]])
            highlight_voltage.setData([x[index]], [voltage[index]])
            highlight_current.setData([x[index]], [current[index]])

            for line in vertical_lines:
                line.setPos(x[index])

            text = (
                f"时间：{time_text[index]}\n"
                f"总功耗：{power[index]:.3f} W\n"
                f"电压：{voltage[index]:.2f} V\n"
                f"电流：{current[index]:.3f} A\n"
                f"累计耗电：{cumulative_energy[index]:.6f} 度"
            )

            self.info_label.setText(
                f"时间：{time_text[index]}    "
                f"总功耗：{power[index]:.3f} W    "
                f"电压：{voltage[index]:.2f} V    "
                f"电流：{current[index]:.3f} A    "
                f"累计耗电：{cumulative_energy[index]:.6f} 度"
            )

            QToolTip.showText(QCursor.pos(), text, self.plot_widget)

        self.hover_proxy = pg.SignalProxy(
            self.plot_widget.scene().sigMouseMoved,
            rateLimit=60,
            slot=lambda event: update_hover(event[0])
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = PowerViewer()
    window.show()
    sys.exit(app.exec())