"""体成分/心血管等非体重指标(Withings 全量数据)。体重仍单独走 weights 表。"""

from datetime import timedelta

from .. import clock, db, events

# Withings measure type → (metric 名, 单位)。见 developer.withings.com Measure 文档
WITHINGS_TYPES: dict[int, tuple[str, str]] = {
    4: ("height", "m"),
    5: ("fat_free_mass", "kg"),
    6: ("fat_ratio", "%"),
    8: ("fat_mass", "kg"),
    9: ("diastolic_bp", "mmHg"),
    10: ("systolic_bp", "mmHg"),
    11: ("heart_rate", "bpm"),
    12: ("temperature", "°C"),
    54: ("spo2", "%"),
    71: ("body_temperature", "°C"),
    73: ("skin_temperature", "°C"),
    76: ("muscle_mass", "kg"),
    77: ("hydration", "kg"),
    88: ("bone_mass", "kg"),
    91: ("pulse_wave_velocity", "m/s"),
    123: ("vo2max", "ml/min/kg"),
    155: ("vascular_age", "years"),
    167: ("nerve_health_score", ""),
    168: ("extracellular_water", "kg"),
    169: ("intracellular_water", "kg"),
    170: ("visceral_fat", "index"),
    173: ("fat_mass_segments", "kg"),
    174: ("muscle_mass_segments", "kg"),
    175: ("hydration_segments", "kg"),
    196: ("electrodermal_activity_feet", ""),
    226: ("basal_metabolic_rate", "kcal"),
    227: ("metabolic_age", "years"),   # 官方文档未公开;与 226 同批推出、实测值 22 与用户年龄吻合
}

# 日语显示名(dashboard / 对话用)
LABELS_JA = {
    "fat_ratio": "体脂肪率", "fat_mass": "脂肪量", "muscle_mass": "筋肉量", "bone_mass": "骨量",
    "hydration": "体水分", "heart_rate": "心拍数", "pulse_wave_velocity": "脈波伝播速度",
    "vascular_age": "血管年齢", "visceral_fat": "内臓脂肪", "basal_metabolic_rate": "基礎代謝",
    "fat_free_mass": "除脂肪体重", "height": "身長", "nerve_health_score": "神経健康スコア",
    "metabolic_age": "代謝年齢",
}


def add(metric: str, value: float, measured_at, unit: str = "", source: str = "withings",
        raw: dict | None = None) -> bool:
    """幂等入库,返回是否新建。"""
    import json

    at = clock.iso(measured_at) if not isinstance(measured_at, str) else measured_at
    conn = db.get_db()
    cur = conn.execute(
        "INSERT OR IGNORE INTO body_metrics (measured_at, metric, value, unit, source, raw, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (at, metric, float(value), unit, source,
         json.dumps(raw, ensure_ascii=False) if raw else None, clock.now_iso()),
    )
    conn.commit()
    return cur.rowcount > 0


def latest(limit_metrics: int = 20) -> dict[str, dict]:
    """每个指标的最新一条:{metric: {value, unit, measured_at}}。"""
    rows = db.get_db().execute(
        "SELECT metric, value, unit, measured_at FROM body_metrics b "
        "WHERE measured_at = (SELECT MAX(measured_at) FROM body_metrics WHERE metric=b.metric) "
        "ORDER BY metric LIMIT ?",
        (limit_metrics,),
    ).fetchall()
    return {r["metric"]: dict(r) for r in rows}


def series(metric: str, days: int = 90) -> list[dict]:
    cutoff = clock.iso(clock.now_utc() - timedelta(days=days))
    rows = db.get_db().execute(
        "SELECT measured_at, value FROM body_metrics WHERE metric=? AND measured_at>=? "
        "ORDER BY measured_at",
        (metric, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def summary_ja() -> str:
    """对话/周报用的一句话摘要,如「体脂肪率 22.5%、筋肉量 60.1kg(8/16)」。"""
    lat = latest()
    parts = []
    for key in ("fat_ratio", "muscle_mass", "visceral_fat", "heart_rate", "vascular_age"):
        if key in lat:
            v = lat[key]
            val = f"{v['value']:.1f}" if v["unit"] not in ("index", "years", "bpm") else f"{v['value']:.0f}"
            parts.append(f"{LABELS_JA.get(key, key)} {val}{v['unit'] if v['unit'] not in ('index',) else ''}")
    return "、".join(parts) if parts else ""
