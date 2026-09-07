from flask import Flask, jsonify, request

app = Flask(__name__)


@app.route('/api/device/status')
def get_status():
    """设备状态"""
    return jsonify({
        "battery": 85,
        "state": "cleaning",
        "clean_area": 12.5,
        "clean_time": 1800,
        "error_code": 0
    })


@app.route('/api/device/map')
def get_map():
    """地图数据"""
    return jsonify({
        "rooms": [
            {"id": 1, "name": "客厅", "area": 20.5},
            {"id": 2, "name": "卧室", "area": 15.2},
            {"id": 3, "name": "厨房", "area": 8.3}
        ],
        "robot_pos": {"x": 230, "y": 156}
    })


@app.route('/api/device/command', methods=['POST'])
def send_command():
    """设备指令"""
    cmd = request.json.get("command") if request.json else None
    responses = {
        "start_clean": {"code": 0, "msg": "started"},
        "pause": {"code": 0, "msg": "paused"},
        "back_to_dock": {"code": 0, "msg": "returning"},
    }
    return jsonify(responses.get(cmd, {"code": 0, "msg": "ok"}))


if __name__ == '__main__':
    app.run(port=5000, debug=False)
