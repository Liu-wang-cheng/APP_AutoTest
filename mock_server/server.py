# -*- coding: utf-8 -*-
"""Flask 假后端: 被测 APP(模拟器)指向它,呈现可预期的界面状态。

由 conftest 的 mock_server fixture 在 --mode mock 下自动拉起。
"""
from flask import Flask, jsonify, request

app = Flask(__name__)


@app.get("/api/device/status")
def status():
    return jsonify(battery=85, state="cleaning", clean_area=12.5,
                   clean_time=1800, error_code=0)


@app.get("/api/device/map")
def map_data():
    return jsonify(rooms=[{"name": "客厅", "area": 20.5},
                          {"name": "卧室", "area": 15.2},
                          {"name": "厨房", "area": 8.3}],
                   robot_pos={"x": 100, "y": 200})


@app.post("/api/device/command")
def command():
    cmd = (request.get_json(silent=True) or {}).get("cmd", "")
    canned = {"start_clean": {"result": "started"},
              "pause": {"result": "paused"},
              "back_to_dock": {"result": "docking"}}
    return jsonify(canned.get(cmd, {"result": "unknown"}))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
