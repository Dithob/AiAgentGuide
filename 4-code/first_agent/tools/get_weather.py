import json
from datetime import datetime, timedelta

def get_weather(city: str, date: str = "today") -> dict:
    """带模拟异常的天气查询"""
    weather_data = {
        "北京": {"today": ("晴", 25), "tomorrow": ("多云", 23)},
        "上海": {"today": ("小雨", 28), "tomorrow": ("阴", 27)},
        "广州": {"today": ("雷阵雨", 31), "tomorrow": ("晴", 33)},
    }

    # 模拟：不支持的城市 → 参数错误
    if city not in weather_data:
        return {
            "error": f"不支持查询 {city} 的天气，目前支持：北京、上海、广州",
            "supported_cities": list(weather_data.keys()),
        }

    # 模拟：随机超时（20%概率）
    import random
    if random.random() < 0.2:
        raise TimeoutError("天气服务响应超时，请稍后重试")

    weather, temp = weather_data[city].get(date, weather_data[city]["today"])
    return {
        "city": city,
        "date": date,
        "weather": weather,
        "temperature": f"{temp}°C",
    }

if __name__ == "__main__":
    print(get_weather("北京", "tomorrow"))

