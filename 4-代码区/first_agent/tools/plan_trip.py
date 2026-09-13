def plan_trip(city: str, weather_condition: str = "", days: int = 1) -> dict:
    """
    根据城市和天气情况规划出行建议

    Args:
        city: 目的地城市
        weather_condition: 天气情况（晴/多云/小雨/雷阵雨等）
        days: 出行天数

    Returns:
        行程建议字典
    """
    trip_data = {
        "北京": {
            "晴": "适合去故宫、颐和园，推荐户外游览",
            "多云": "适合逛博物馆、胡同，室内外均可",
            "小雨": "建议参观国家博物馆、798艺术区，以室内为主",
            "雷阵雨": "强烈建议室内活动：故宫室内展区、国家大剧院",
        },
        "上海": {
            "晴": "外滩散步、豫园游览、迪士尼乐园",
            "多云": "南京路逛街、田子坊艺术区",
            "小雨": "上海博物馆、环球金融中心观光厅",
            "雷阵雨": "室内商场、上海大剧院",
        },
        "广州": {
            "晴": "白云山登山、珠江夜游",
            "多云": "陈家祠、沙面岛散步",
            "小雨": "广州图书馆、广东省博物馆",
            "雷阵雨": "天河城购物中心、室内美食探店",
        },
    }

    if city not in trip_data:
        return {
            "error": f"暂不支持 {city} 的行程规划",
            "supported_cities": list(trip_data.keys()),
        }

    if not weather_condition:
        return {
            "city": city,
            "note": "请先查询天气，我再给出更精准的行程建议",
            "general_tip": trip_data[city].get("晴", "建议户外游览"),
        }

    recommendation = trip_data[city].get(
        weather_condition,
        "建议根据天气灵活安排室内外活动"
    )

    return {
        "city": city,
        "weather_condition": weather_condition,
        "recommendation": recommendation,
        "days": days,
    }

if __name__ == "__main__":
    print(plan_trip("北京", "雷阵雨", 2))