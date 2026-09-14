tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市指定日期的天气信息，包括天气状况和温度",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，如 北京、上海、广州"
                    },
                    "date": {
                        "type": "string",
                        "description": "日期，可以是 today、tomorrow 或 YYYY-MM-DD",
                        "default": "today"
                    }
                },
                "required": ["city"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "plan_trip",
            "description": "根据城市和天气情况规划出行建议，需要天气信息作为输入",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "目的地城市，如 北京、上海、广州"
                    },
                    "weather_condition": {
                        "type": "string",
                        "description": "天气状况，如 晴、多云、小雨、雷阵雨"
                    },
                    "days": {
                        "type": "integer",
                        "description": "出行天数，默认1天",
                        "default": 1
                    }
                },
                "required": ["city"]
            }
        }
    }
]