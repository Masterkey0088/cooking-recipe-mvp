# -*- coding: utf-8 -*-
# ごはんの神様に相談だ！ / Streamlit App
# 方式A：Secretsの APP_MODE によりベータ/開発/本番を切替
# 必須Secrets: OPENAI_API_KEY（使う場合）、任意: APP_MODE, APP_ACCESS_CODE

from __future__ import annotations
import os, re, json, math, random
from typing import List, Optional, Dict, Tuple

import streamlit as st
from pydantic import BaseModel, Field

# ------------------------------------------------------------
# App mode & feature flags
# ------------------------------------------------------------
APP_MODE = (st.secrets.get("APP_MODE") or os.getenv("APP_MODE") or "beta").lower()
IS_DEV = APP_MODE in ("dev", "development")
IS_PROD = APP_MODE in ("prod", "production")

APP_TITLE = "ごはんの神様に相談だ！" + ("（開発版）" if IS_DEV else ("（本番）" if IS_PROD else "（ベータ版）"))
st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title(f"🍳 {APP_TITLE}")

FEATURES = {
    "ENABLE_IMAGE_UI": False,         # 画像UI（今は非表示）
    "TEMPERATURE": 0.4 if not IS_DEV else 0.6,
    "SHOW_DEBUG_PANEL": IS_DEV,

    # 品質関連
    "ENABLE_QUALITY_FILTER": True,
    "MAX_QUALITY_RETRY": 2 if not IS_DEV else 3,
    "KEEP_AT_LEAST_ONE": True,

    # 週モード：再最適化の試行回数
    "WEEK_REPLAN_ATTEMPTS": 2,
}

# ------------------------------------------------------------
# （任意）アクセスコードロック
# ------------------------------------------------------------
ACCESS_CODE = st.secrets.get("APP_ACCESS_CODE") or os.getenv("APP_ACCESS_CODE")
if ACCESS_CODE:
    if not st.session_state.get("auth_ok"):
        st.info("このアプリはアクセスコードが必要です。")
        code = st.text_input("アクセスコード", type="password")
        if st.button("Enter", use_container_width=True):
            if code == ACCESS_CODE:
                st.session_state["auth_ok"] = True
                st.rerun()
            else:
                st.error("アクセスコードが違います")
                st.stop()
        st.stop()

# ============================================================
# データモデル
# ============================================================
class Ingredient(BaseModel):
    name: str
    amount: Optional[str] = None
    is_optional: bool = False
    substitution: Optional[str] = None

class Step(BaseModel):
    text: str

class Recipe(BaseModel):
    recipe_title: str = Field(..., alias="title")
    servings: int = 2
    total_time_min: Optional[int] = None
    difficulty: Optional[str] = None
    ingredients: List[Ingredient]
    steps: List[Step]
    equipment: Optional[List[str]] = None

class RecipeSet(BaseModel):
    recommendations: List[Recipe]

# 週プラン用データ（軽量）
class DayPlan(BaseModel):
    day_index: int
    recipe: Recipe
    est_cost: int  # 円（概算）

# ============================================================
# ユーティリティ：テキスト整形・材料正規化
# ============================================================
_STEP_PREFIX_RE = re.compile(
    r"^\s*(?:STEP\s*[0-9０-９]+[:：\-\s]*|[0-9０-９]+[\.．、\)）]\s*|[①-⑳]\s*)"
)
def strip_step_prefix(text: str) -> str:
    return _STEP_PREFIX_RE.sub('', text or '').strip()

TSP_IN_TBSP = 3.0

PROTEIN_G_PER_SERV = {
    "鶏むね肉": 100, "鶏もも肉": 100, "豚肉": 100, "牛肉": 100, "ひき肉": 100,
    "鮭": 90, "さば": 90, "ツナ": 70, "ベーコン": 30, "ハム": 30, "豆腐": 150, "木綿豆腐": 150, "絹ごし豆腐": 150, "卵": 50
}
VEG_G_PER_SERV = {
    "玉ねぎ": 50, "ねぎ": 10, "長ねぎ": 20, "キャベツ": 80, "にんじん": 40,
    "じゃがいも": 80, "なす": 60, "ピーマン": 40, "もやし": 100, "ブロッコリー": 70,
    "きのこ": 60, "しめじ": 60, "えのき": 60, "トマト": 80, "青菜": 70, "小松菜": 70, "ほうれん草": 70
}
COND_TSP_PER_SERV = {
    "塩": 0.125, "砂糖": 0.5, "しょうゆ": 1.0, "醤油": 1.0, "みりん": 1.0, "酒": 1.0,
    "酢": 1.0, "コチュジャン": 0.5, "味噌": 1.5, "味の素": 0.25, "顆粒だし": 0.5
}
OIL_TSP_PER_SERV = {"サラダ油": 1.0, "ごま油": 0.5, "オリーブオイル": 1.0}
PIECE_PER_SERV = {"卵": "1個", "にんにく": "0.5片", "生姜": "0.5片"}

SPICY_WORDS = ["一味", "七味", "豆板醤", "コチュジャン", "ラー油", "唐辛子", "粉唐辛子"]

_num_re = re.compile(r'([0-9]+(?:\.[0-9]+)?)')
def _has_number(s: str) -> bool:
    return bool(_num_re.search(s or ""))

def _round_tsp_to_pretty(tsp: float) -> str:
    if tsp <= 0.15:
        return "少々"
    tbsp = tsp / TSP_IN_TBSP
    if tbsp >= 1.0:
        val = round(tbsp * 2) / 2
        return f"大さじ{val:g}"
    else:
        val = round(tsp * 2) / 2
        return f"小さじ{val:g}"

def _grams_to_pretty(g: int) -> str:
    if g < 60: step = 10
    elif g < 150: step = 25
    else: step = 50
    pretty = int(round(g / step) * step)
    return f"{pretty}g"

def _guess_amount(name: str, servings: int) -> str:
    for key, per in PIECE_PER_SERV.items():
        if key in name:
            m = _num_re.search(per)
            num = float(m.group(1)) if m else 1.0
            unit = per.replace(str(num).rstrip('0').rstrip('.'), '')
            total = num * servings
            if abs(total - int(total)) < 1e-6:
                return f"{int(total)}{unit}"
            return f"{total:g}{unit}"
    for key, g in PROTEIN_G_PER_SERV.items():
        if key in name:
            return _grams_to_pretty(int(g * servings))
    for key, g in VEG_G_PER_SERV.items():
        if key in name:
            return _grams_to_pretty(int(g * servings))
    for key, tsp in OIL_TSP_PER_SERV.items():
        if key in name:
            return _round_tsp_to_pretty(tsp * servings)
    for key, tsp in COND_TSP_PER_SERV.items():
        if key in name:
            return _round_tsp_to_pretty(tsp * servings)
    if any(k in name for k in ["胡椒", "こしょう", "黒胡椒", "一味", "七味", "ラー油"]):
        return "少々"
    return "適量"

_QTY_IN_NAME_RE = re.compile(
    r'(?:^|\s)('
    r'(?:小さじ|大さじ)\s*\d+(?:\.\d+)?'
    r'|(?:\d+(?:\.\d+)?)\s*(?:g|グラム|kg|㎏|ml|mL|L|cc|カップ|cup|個|片|枚|本)'
    r'|少々|適量'
    r')(?=\s|$)'
)
def split_quantity_from_name(name: str) -> tuple[str, Optional[str]]:
    txt = name or ""
    m = _QTY_IN_NAME_RE.search(txt)
    qty = m.group(1) if m else None
    base = _QTY_IN_NAME_RE.sub(" ", txt).strip()
    base = re.sub(r'\s{2,}', ' ', base)
    return (base or txt), qty

def sanitize_amount(amount: Optional[str]) -> Optional[str]:
    if not amount:
        return None
    a = amount.strip().replace("．", ".").replace(".0", "")
    if a in {"小さじ0", "大さじ0", "0g", "0個", "0片", "0枚", "0本", "0cc"}:
        return "少々"
    return a

def amount_to_unit_value(amount: str) -> tuple[str, float]:
    if not amount:
        return ("", 0.0)
    a = amount.replace("．",".").strip().lower()
    m = re.search(r'大さじ\s*(\d+(?:\.\d+)?)', a)
    if m: return ("tbsp", float(m.group(1)))
    m = re.search(r'小さじ\s*(\d+(?:\.\d+)?)', a)
    if m: return ("tsp", float(m.group(1)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*g', a)
    if m: return ("g", float(m.group(1)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*個', a)
    if m: return ("piece", float(m.group(1)))
    return ("", 0.0)

def unit_value_to_amount(u: str, v: float) -> str:
    if u == "tbsp":
        v = round(v*2)/2
        if v <= 0: return "少々"
        return f"大さじ{v:g}"
    if u == "tsp":
        v = round(v*2)/2
        if v <= 0: return "少々"
        return f"小さじ{v:g}"
    if u == "g":
        if v <= 0: return "少々"
        return _grams_to_pretty(int(round(v)))
    if u == "piece":
        if abs(v - int(v)) < 1e-6:
            return f"{int(v)}個"
        return f"{v:g}個"
    return sanitize_amount(str(v)) or "適量"

def is_condiment(name: str) -> bool:
    KEYS = ["塩","砂糖","しょうゆ","醤油","みりん","酒","味噌","酢","ごま油","オリーブオイル","油","バター","だし","顆粒だし"]
    return any(k in name for k in KEYS)

def is_spicy(name: str) -> bool:
    return any(k in name for k in SPICY_WORDS)

def adjust_child_friendly_amount(name: str, amount: str, factor: float = 0.8) -> str:
    if not amount:
        return amount
    u, v = amount_to_unit_value(amount)
    if is_spicy(name):
        return "少々（大人は後がけ）"
    if is_condiment(name):
        if u in {"tbsp","tsp","g"}:
            nv = v * factor
            return unit_value_to_amount(u, nv)
    return amount

def normalize_ingredients(ings: List[Ingredient], servings: int, child_mode: bool = False, child_factor: float = 0.8) -> List[Ingredient]:
    fixed: List[Ingredient] = []
    for it in ings:
        base_name, qty_in_name = split_quantity_from_name(it.name)
        amt = sanitize_amount(getattr(it, "amount", None)) or qty_in_name or ""
        if (not amt) or ("適量" in amt) or (not _has_number(amt) and "少々" not in amt):
            amt = _guess_amount(base_name, servings)
        amt = sanitize_amount(amt) or "適量"
        if child_mode:
            amt = adjust_child_friendly_amount(base_name, amt, child_factor)
        fixed.append(Ingredient(
            name=base_name,
            amount=amt,
            is_optional=getattr(it, "is_optional", False),
            substitution=getattr(it, "substitution", None),
        ))
    return fixed

# ============================================================
# 器具推定（簡易）
# ============================================================
_TOOL_RULES = [
    (r"(切る|刻む|みじん|千切り|輪切り|そぎ切り)", ["包丁", "まな板"]),
    (r"(混ぜ|和え|ほぐし|溶き卵|衣を作る)", ["ボウル", "菜箸"]),
    (r"(炒め|焼き色|ソテー|香りが立つまで)", ["フライパン", "フライ返し"]),
    (r"(茹で|ゆで|湯が|下茹で)", ["鍋（湯用）", "ザル"]),
    (r"(煮|煮込|煮立|弱火|中火|強火|沸騰)", ["鍋", "菜箸"]),
    (r"(電子レンジ|レンジ|600W|500W)", ["電子レンジ", "耐熱容器", "ラップ"]),
]
_MEASURE_RE = re.compile(r"(小さじ|大さじ|カップ|cup|cc|ml|mL|L|ℓ)")
def infer_tools_from_recipe(rec: Recipe) -> List[str]:
    ings_txt = "、".join([i.name for i in rec.ingredients])
    steps_txt = "。".join([s.text for s in rec.steps])
    txt = f"{ings_txt}\n{steps_txt}"
    tools: List[str] = []
    for pattern, add in _TOOL_RULES:
        if re.search(pattern, txt):
            for t in add:
                if t not in tools:
                    tools.append(t)
    if _MEASURE_RE.search(txt):
        for t in ["計量スプーン"]:
            if t not in tools:
                tools.append(t)
    if not tools:
        tools = ["包丁", "まな板", "フライパンまたは鍋", "計量スプーン"]
    return tools

# ============================================================
# 品質チェック（OKのみ表示に使う）
# ============================================================
HEAT_WORDS = ["弱火", "中火", "強火", "沸騰", "余熱", "レンジ", "600W", "500W"]
SEASONINGS = ["塩", "砂糖", "しょうゆ", "醤油", "みりん", "酒", "味噌", "酢", "ごま油", "オリーブオイル", "バター", "だし"]

def quality_check(rec) -> tuple[bool, List[str]]:
    warns: List[str] = []
    if len(getattr(rec, "ingredients", []) or []) < 3:
        warns.append("材料が少なすぎます（3品以上を推奨）")
    if len(getattr(rec, "steps", []) or []) < 3:
        warns.append("手順が少なすぎます（3ステップ以上を推奨）")
    step_text = "。".join([getattr(s, "text", "") for s in (rec.steps or [])])
    if not any(w in step_text for w in HEAT_WORDS):
        warns.append("加熱の記述がありません（弱火/中火/強火 や レンジ時間の明示を推奨）")
    ing_txt = "、".join([f"{getattr(i, 'name', '')} {getattr(i, 'amount', '')}" for i in (rec.ingredients or [])])
    if not any(s in ing_txt for s in SEASONINGS):
        warns.append("基本的な調味が見当たりません（塩・しょうゆ・みりん等）")
    if "適量" in ing_txt:
        warns.append("“適量”が含まれています（できるだけ小さじ/大さじ/グラム表記に）")
    ok = (len(warns) == 0)
    return ok, warns

def _filter_passed_recipes(recs: List[Recipe]) -> List[Recipe]:
    return [r for r in recs if quality_check(r)[0]]

# ============================================================
# 栄養＆価格テーブル（概算）
# ============================================================
NUTRI_PROFILES = {
    "ふつう":   {"kcal": (500, 800), "protein_g": (20, 35), "salt_g": (0, 2.5)},
    "ダイエット": {"kcal": (350, 600), "protein_g": (25, 40), "salt_g": (0, 2.0)},
    "がっつり": {"kcal": (700,1000), "protein_g": (35, 55), "salt_g": (0, 3.0)},
    "減塩":     {"kcal": (500, 800), "protein_g": (20, 35), "salt_g": (0, 2.0)},
}

FOODS = {
    # 100g基準（調味料は大さじ基準）
    "鶏むね肉": {"kcal":120,"protein_g":23,"fat_g":2, "carb_g":0, "salt_g":0, "yen_per_100g": 68},
    "鶏もも肉": {"kcal":200,"protein_g":17,"fat_g":14,"carb_g":0,"salt_g":0, "yen_per_100g": 98},
    "豚肉":     {"kcal":242,"protein_g":20,"fat_g":19,"carb_g":0,"salt_g":0, "yen_per_100g": 128},
    "牛肉":     {"kcal":250,"protein_g":20,"fat_g":19,"carb_g":0,"salt_g":0, "yen_per_100g": 198},
    "ひき肉":   {"kcal":230,"protein_g":19,"fat_g":17,"carb_g":0,"salt_g":0, "yen_per_100g": 118},
    "鮭":       {"kcal":200,"protein_g":22,"fat_g":12,"carb_g":0,"salt_g":0, "yen_per_100g": 198},
    "さば":     {"kcal":240,"protein_g":20,"fat_g":19,"carb_g":0,"salt_g":0, "yen_per_100g": 158},
    "木綿豆腐": {"kcal":72, "protein_g":7, "fat_g":4, "carb_g":2, "salt_g":0, "yen_per_piece": 62, "piece_g":300},
    "絹ごし豆腐":{"kcal":56,"protein_g":5, "fat_g":3, "carb_g":2, "salt_g":0, "yen_per_piece": 62, "piece_g":300},
    "卵":       {"kcal":150,"protein_g":12,"fat_g":10,"carb_g":0,"salt_g":0, "yen_per_piece": 25, "piece_g":50},

    "キャベツ": {"kcal":23,"protein_g":1,"fat_g":0,"carb_g":5,"salt_g":0, "yen_per_100g": 25},
    "玉ねぎ":   {"kcal":37,"protein_g":1,"fat_g":0,"carb_g":9,"salt_g":0, "yen_per_piece": 40, "piece_g":180},
    "にんじん": {"kcal":37,"protein_g":1,"fat_g":0,"carb_g":9,"salt_g":0, "yen_per_100g": 28},
    "じゃがいも":{"kcal":76,"protein_g":2,"fat_g":0,"carb_g":17,"salt_g":0, "yen_per_100g": 25},
    "なす":     {"kcal":22,"protein_g":1,"fat_g":0,"carb_g":5,"salt_g":0, "yen_per_100g": 40},
    "もやし":   {"kcal":14,"protein_g":2,"fat_g":0,"carb_g":3,"salt_g":0, "yen_per_100g": 20},

    # 調味料（大さじ基準：おおよそ）
    "しょうゆ": {"kcal":13, "protein_g":1.4,"fat_g":0,"carb_g":1.2,"salt_g":2.6, "yen_per_tbsp": 10},
    "みりん":   {"kcal":43, "protein_g":0, "fat_g":0,"carb_g":7.2,"salt_g":0,   "yen_per_tbsp": 10},
    "酒":       {"kcal":11, "protein_g":0, "fat_g":0,"carb_g":0.5,"salt_g":0,   "yen_per_tbsp": 8},
    "砂糖":     {"kcal":35, "protein_g":0, "fat_g":0,"carb_g":9,  "salt_g":0,   "yen_per_tbsp": 5},
    "味噌":     {"kcal":33, "protein_g":2, "fat_g":1,"carb_g":4,  "salt_g":0.9, "yen_per_tbsp": 15},
    "ごま油":   {"kcal":111,"protein_g":0, "fat_g":12.6,"carb_g":0,"salt_g":0, "yen_per_tbsp": 18},
    "オリーブオイル":{"kcal":111,"protein_g":0,"fat_g":12.6,"carb_g":0,"salt_g":0,"yen_per_tbsp": 20},
    "塩":       {"kcal":0,  "protein_g":0, "fat_g":0,"carb_g":0,  "salt_g":6.0, "yen_per_tsp": 2},
}

# ---- 栄養推定 ----
def amount_to_grams_or_spoons(amount: str) -> tuple[str, float]:
    if not amount: return ("g", 0.0)
    a = amount.replace("．",".").strip().lower()
    m = re.search(r'(\d+(?:\.\d+)?)\s*(g|グラム)', a)
    if m: return ("g", float(m.group(1)))
    m = re.search(r'大さじ\s*(\d+(?:\.\d+)?)', a)
    if m: return ("tbsp", float(m.group(1)))
    m = re.search(r'小さじ\s*(\d+(?:\.\d+)?)', a)
    if m: return ("tsp", float(m.group(1)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*個', a)
    if m: return ("piece", float(m.group(1)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*片', a)
    if m: return ("piece", float(m.group(1)) * 0.5)
    return ("g", 0.0)

def tbsp_from_tsp(x: float) -> float: return x / 3.0

def estimate_nutrition(rec) -> dict:
    total = {"kcal":0.0,"protein_g":0.0,"fat_g":0.0,"carb_g":0.0,"salt_g":0.0}
    for ing in rec.ingredients:
        name = ing.name
        amt_str = ing.amount or ""
        unit, val = amount_to_grams_or_spoons(amt_str)

        key = None
        for k in FOODS.keys():
            if k in name:
                key = k; break
        if not key:
            continue

        base = FOODS[key].copy()
        factor = 0.0
        if unit == "g":
            factor = val / 100.0
        elif unit == "tbsp":
            if key in ["しょうゆ","みりん","酒","味噌","ごま油","オリーブオイル","塩"]:
                factor = val  # 大さじ=1単位として扱う栄養テーブル
            else:
                factor = (val * 15.0) / 100.0
        elif unit == "tsp":
            if key == "塩":
                factor = val  # 小さじ=1単位
            elif key in ["しょうゆ","みりん","酒","味噌","ごま油","オリーブオイル"]:
                factor = tbsp_from_tsp(val)
            else:
                factor = (val * 5.0) / 100.0
        elif unit == "piece":
            piece_g = 0
            if "卵" in name: piece_g = 50
            elif "にんにく" in name: piece_g = 5
            else: piece_g = 30
            factor = (piece_g * val) / 100.0
        else:
            continue

        for k in total:
            total[k] += base[k] * factor

    serv = max(1, getattr(rec, "servings", 1))
    for k in total:
        total[k] = round(total[k] / serv, 1)
    return total

def score_against_profile(nutri: dict, profile_name: str) -> dict:
    prof = NUTRI_PROFILES.get(profile_name, NUTRI_PROFILES["ふつう"])
    def mark(val, rng):
        lo, hi = rng
        if val < lo*0.9: return "△"
        if lo <= val <= hi: return "◎"
        if val <= hi*1.15: return "△"
        return "⚠"
    return {
        "kcal":      mark(nutri["kcal"],      prof["kcal"]),
        "protein_g": mark(nutri["protein_g"], prof["protein_g"]),
        "salt_g":    mark(nutri["salt_g"],    prof["salt_g"]),
    }

# ---- 価格推定 ----
def estimate_cost_yen(rec: Recipe, price_factor: float = 1.0) -> int:
    """材料の概算コスト（円）。豆腐/卵/玉ねぎなどは個数単価、その他は100g単価を使う。調味料は小さく計上。"""
    total = 0.0
    for ing in rec.ingredients:
        name = ing.name
        amt = ing.amount or ""
        unit, val = amount_to_grams_or_spoons(amt)

        key = None
        for k in FOODS.keys():
            if k in name:
                key = k; break
        if not key:
            # 未知の野菜は薄く見る
            if unit == "g":
                total += (val/100.0) * 30 * price_factor
            continue

        meta = FOODS[key]
        if "yen_per_piece" in meta:
            if unit == "piece":
                total += meta["yen_per_piece"] * val * price_factor
            elif unit == "g":
                # g指定でも個体に換算
                piece_g = meta.get("piece_g", 100)
                pieces = val / piece_g
                total += meta["yen_per_piece"] * pieces * price_factor
            else:
                # ざっくり1個扱い
                total += meta["yen_per_piece"] * price_factor
        elif "yen_per_100g" in meta:
            if unit == "g":
                total += (val/100.0) * meta["yen_per_100g"] * price_factor
            elif unit in ("tbsp","tsp","piece"):
                # 重量換算（雑に）
                grams = 15*val if unit=="tbsp" else (5*val if unit=="tsp" else 50*val)
                total += (grams/100.0) * meta["yen_per_100g"] * price_factor
            else:
                total += meta["yen_per_100g"] * price_factor
        elif "yen_per_tbsp" in meta:
            if unit == "tbsp":
                total += meta["yen_per_tbsp"] * val * price_factor
            elif unit == "tsp":
                total += meta["yen_per_tbsp"] * (val/3.0) * price_factor
            else:
                total += meta["yen_per_tbsp"] * price_factor

        # ごく小量の調味料はカウントしない
        if "少々" in amt:
            total += 0

    return int(round(total))

# ============================================================
# OpenAI 呼び出し（JSON生成＋フォールバック）
# ============================================================
USE_OPENAI = True
try:
    from openai import OpenAI
    _client = OpenAI() if (USE_OPENAI and (os.getenv("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY"))) else None
except Exception:
    _client = None

PROMPT_TMPL = (
    "You are a helpful Japanese cooking assistant.\n"
    "Given ingredients, servings, theme, genre and max time, propose 1–3 Japanese home recipes.\n"
    "Output strict JSON matching this schema in UTF-8 (no markdown):\n"
    "{\n"
    '  "recommendations": [\n'
    "    {\n"
    '      "title": string,\n'
    '      "servings": int,\n'
    '      "total_time_min": int,\n'
    '      "difficulty": string,\n'
    '      "ingredients": [ {\n'
    '        "name": string,\n'
    '        "amount": string | null,\n'
    '        "is_optional": boolean,\n'
    '        "substitution": string | null\n'
    "      } ],\n"
    '      "steps": [ { "text": string } ],\n'
    '      "equipment": string[] | null\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "Notes: Avoid vague amounts like '適量' when possible; prefer grams and 大さじ/小さじ. "
    "Provide cooking times and heat levels (弱火/中火/強火) explicitly. Avoid steps that cannot be executed in a home kitchen.\n"
)

def generate_recipes(
    ingredients: List[str],
    servings: int,
    theme: str,
    genre: str,
    max_minutes: int,
    want_keyword: str = "",
    avoid_keywords: List[str] | None = None,
    child_mode: bool = False,
    cheap_hint: bool = False,
    hint_protein: str = ""
) -> RecipeSet:
    avoid_keywords = avoid_keywords or []

    if _client is not None:
        try:
            avoid_line = ("除外: " + ", ".join(avoid_keywords)) if avoid_keywords else "除外: なし"
            want_line  = ("希望: " + want_keyword) if want_keyword else "希望: なし"
            theme_line = f"テーマ: {theme}\n" if theme else ""
            genre_line = f"ジャンル: {genre}\n" if genre else ""
            child_line = "子ども配慮: はい（辛味抜き・塩分-20%・一口大・やわらかめ・酒は十分加熱）\n" if child_mode else ""
            cheap_line = "価格優先: はい（安価な食材・鶏むね/豆腐/卵/もやし/キャベツ等を優先）\n" if cheap_hint else ""
            protein_line = f"主たるたんぱく源の希望: {hint_protein}\n" if hint_protein else ""

            user_msg = (
                f"食材: {', '.join(ingredients) if ingredients else '（未指定）'}\n"
                f"人数: {servings}人\n"
                f"{theme_line}{genre_line}{child_line}{cheap_line}{protein_line}"
                f"最大調理時間: {max_minutes}分\n"
                f"{want_line}\n{avoid_line}\n"
                "要件:\n"
                "- 出力はSTRICTなJSONのみ（マークダウン不可）\n"
                "- 除外キーワードを含む料理名は絶対に出さない\n"
                "- 量はできるだけ具体（g, 小さじ/大さじ/個・片）に\n"
            )
            resp = _client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=FEATURES["TEMPERATURE"],
                messages=[
                    {"role": "system", "content": PROMPT_TMPL},
                    {"role": "user", "content": user_msg},
                ],
            )
            text = resp.choices[0].message.content or "{}"
            data = json.loads(text)
            parsed = RecipeSet.model_validate(data)
            return parsed
        except Exception as e:
            st.info(f"LLMの構造化生成に失敗したためフォールバックします: {e}")

    # Fallback — 最低1件
    base_ings = [Ingredient(name=x) for x in ingredients[:6]] or [Ingredient(name="鶏むね肉"), Ingredient(name="キャベツ")]
    steps = [
        Step(text="材料を食べやすい大きさに切る"),
        Step(text="フライパンで油を熱し、肉と野菜を炒める"),
        Step(text="しょうゆ・みりん・酒で味付けして全体を絡める（中火）"),
    ]
    title = (want_keyword or f"{hint_protein}の簡単炒め").strip() or "かんたん炒め"
    rec = Recipe(
        title=title, servings=servings, total_time_min=min(20, max_minutes),
        difficulty="かんたん", ingredients=base_ings, steps=steps, equipment=None
    )
    return RecipeSet(recommendations=[rec])

# ============================================================
# 週プラン生成（予算逆算＆再最適化）
# ============================================================
PROTEIN_ROTATION_DEFAULT = ["鶏むね肉","豚肉","豆腐","鮭","鶏もも肉","卵","さば"]
PROTEIN_ROTATION_CHEAP   = ["鶏むね肉","豆腐","卵","豚肉","もやし入り","鶏むね肉","豆腐"]

def plan_week(
    num_days: int,
    budget_yen: int,
    servings: int,
    theme: str,
    genre: str,
    max_minutes: int,
    price_factor: float,
    child_mode: bool,
    want_keyword: str,
    avoid_keywords: List[str],
    profile_name: str,
    prefer_cheap: bool = False
) -> tuple[List[DayPlan], int, Optional[int], dict]:
    """
    Returns: (plans, total_cost, feasible_budget_if_any, week_nutrition_summary)
    - feasible_budget_if_any: 予算未達なら最安構成でもの必要額
    """
    rotation = PROTEIN_ROTATION_CHEAP if prefer_cheap else PROTEIN_ROTATION_DEFAULT

    def make_day(hint_protein: str, cheap_hint: bool) -> DayPlan:
        data = generate_recipes(
            ingredients=[], servings=servings,
            theme=theme, genre=genre, max_minutes=max_minutes,
            want_keyword=want_keyword, avoid_keywords=avoid_keywords,
            child_mode=child_mode, cheap_hint=cheap_hint, hint_protein=hint_protein
        )
        recs = data.recommendations or []
        # 品質フィルタ（OK優先）
        passed = _filter_passed_recipes(recs) if FEATURES["ENABLE_QUALITY_FILTER"] else recs
        chosen = (passed[0] if passed else (recs[0] if recs else None))
        if not chosen:
            # 最低限フォールバック
            chosen = Recipe(
                title=f"{hint_protein or '鶏むね肉'}の炒めもの",
                servings=servings, total_time_min=min(20, max_minutes),
                difficulty="かんたん",
                ingredients=[Ingredient(name=hint_protein or "鶏むね肉"), Ingredient(name="キャベツ")],
                steps=[Step(text="材料を切って炒め、調味する（中火）")],
                equipment=None
            )
        # 正規化
        chosen.ingredients = normalize_ingredients(chosen.ingredients, chosen.servings, child_mode=child_mode, child_factor=0.8 if child_mode else 1.0)
        est_cost = estimate_cost_yen(chosen, price_factor=price_factor)
        return DayPlan(day_index=0, recipe=chosen, est_cost=est_cost)

    # まず通常ローテーションで組む
    plans: List[DayPlan] = []
    for i in range(num_days):
        hint = rotation[i % len(rotation)]
        dp = make_day(hint, cheap_hint=False)
        dp.day_index = i+1
        plans.append(dp)

    total_cost = sum(p.est_cost for p in plans)

    # 予算内ならOK
    if total_cost <= budget_yen:
        week_summary = weekly_nutrition_summary(plans, profile_name)
        return plans, total_cost, None, week_summary

    # 予算超過 → 高コスト日を安価生成で差し替えてみる
    attempts = FEATURES["WEEK_REPLAN_ATTEMPTS"]
    for _ in range(attempts):
        plans.sort(key=lambda x: x.est_cost, reverse=True)
        # 上位2日を安価ヒントで再生成
        changed = False
        for j in range(min(2, len(plans))):
            i = plans[j].day_index
            cheap_hint = True
            cheap_protein = PROTEIN_ROTATION_CHEAP[(i-1) % len(PROTEIN_ROTATION_CHEAP)]
            new_dp = make_day(cheap_protein, cheap_hint=True)
            new_dp.day_index = i
            if new_dp.est_cost < plans[j].est_cost:
                plans[j] = new_dp
                changed = True
        total_cost = sum(p.est_cost for p in plans)
        if total_cost <= budget_yen:
            break
        if not changed:
            break

    if total_cost <= budget_yen:
        week_summary = weekly_nutrition_summary(plans, profile_name)
        return plans, total_cost, None, week_summary

    # それでも無理なら「実現可能予算」を提示
    week_summary = weekly_nutrition_summary(plans, profile_name)
    return plans, total_cost, total_cost, week_summary

def weekly_nutrition_summary(plans: List[DayPlan], profile_name: str) -> dict:
    """週合計→1日平均にして◎/△/⚠スコア"""
    tot = {"kcal":0.0,"protein_g":0.0,"fat_g":0.0,"carb_g":0.0,"salt_g":0.0}
    days = max(1, len(plans))
    for p in plans:
        nutri = estimate_nutrition(p.recipe)
        for k in tot: tot[k] += nutri[k]
    avg = {k: round(v/days,1) for k,v in tot.items()}
    score = score_against_profile(avg, profile_name)
    return {"avg": avg, "score": score}

# ============================================================
# UI：1日/1週間 切替フォーム（画像UIは非表示）
# ============================================================
with st.form("inputs", clear_on_submit=False, border=True):
    mode = st.radio("提案範囲", ["1日分", "1週間分"], horizontal=True)

    st.text_input("冷蔵庫の食材（カンマ区切り・任意）", key="ingredients", placeholder="例）豚肉, キャベツ, ねぎ")

    c1, c2, c3 = st.columns([1,1,1])
    with c1:
        st.slider("人数（合計）", 1, 8, 2, 1, key="servings")
    with c2:
        themes = ["（お任せ）", "時短", "節約", "栄養重視", "子ども向け", "おもてなし"]
        st.selectbox("テーマ", themes, index=0, key="theme")
    with c3:
        genres = ["（お任せ）", "和風", "洋風", "中華風", "韓国風", "エスニック"]
        st.selectbox("ジャンル", genres, index=0, key="genre")

    st.slider("最大調理時間（分）", 5, 90, 30, 5, key="max_minutes")

    st.text_input("作りたい料理名・キーワード（任意）", key="want_keyword", placeholder="例）麻婆豆腐、ナスカレー")
    st.text_input("除外したい料理名・キーワード（カンマ区切り・任意）", key="avoid_keywords", placeholder="例）麻婆豆腐, カレー")

    # 子ども配慮
    st.checkbox("子ども向け配慮（辛味抜き・塩分ひかえめ・食べやすく）", value=False, key="child_mode")

    # 栄養プロファイル
    st.selectbox("栄養目安プロファイル", list(NUTRI_PROFILES.keys()), index=0, key="nutri_profile")

    # 週モード設定
    if mode == "1週間分":
        c4, c5, c6 = st.columns([1,1,1])
        with c4:
            st.number_input("今週の予算（円）", min_value=1000, step=500, value=8000, key="week_budget")
        with c5:
            st.slider("今週つくる回数（外食・予定は除外）", 3, 7, 5, 1, key="week_days")
        with c6:
            st.select_slider("価格感（地域/体感係数）", options=["安め","ふつう","やや高め","高め"], value="ふつう", key="price_profile")
        st.checkbox("節約優先で組む（鶏むね・豆腐中心）", value=False, key="prefer_cheap")

    submitted = st.form_submit_button("提案を作成", use_container_width=True)

# ------------------------------------------------------------
# 入力整形
# ------------------------------------------------------------
if not submitted:
    st.stop()

ing_text = st.session_state.get("ingredients", "") or ""
ingredients_raw = [s for s in (t.strip() for t in re.split(r"[、,]", ing_text)) if s]
theme = st.session_state.get("theme", "（お任せ）")
genre = st.session_state.get("genre", "（お任せ）")
if theme == "（お任せ）": theme = ""
if genre == "（お任せ）": genre = ""

servings = int(st.session_state.get("servings", 2))
max_minutes = int(st.session_state.get("max_minutes", 30))
want_keyword = (st.session_state.get("want_keyword") or "").strip()
avoid_keywords = [s for s in (t.strip() for t in re.split(r"[、,]", st.session_state.get("avoid_keywords") or "")) if s]
child_mode = bool(st.session_state.get("child_mode", False))
nutri_profile = st.session_state.get("nutri_profile","ふつう")

# 価格係数
price_profile = st.session_state.get("price_profile", "ふつう")
price_factor = {"安め":0.9, "ふつう":1.0, "やや高め":1.1, "高め":1.2}.get(price_profile, 1.0)

# ============================================================
# 分岐：1日 / 1週間
# ============================================================
if mode == "1日分":
    try:
        data = generate_recipes(
            ingredients_raw, servings, theme, genre, max_minutes,
            want_keyword=want_keyword, avoid_keywords=avoid_keywords,
            child_mode=child_mode
        )
    except Exception as e:
        st.error(f"レシピ生成に失敗しました: {e}")
        st.stop()

    recs = data.recommendations or []
    if FEATURES["ENABLE_QUALITY_FILTER"]:
        # 希望優先 → 品質OK → その他
        if want_keyword:
            matched = [r for r in recs if want_keyword.lower() in r.recipe_title.lower()]
            others  = [r for r in recs if r not in matched]
            recs = matched + others
        recs = _filter_passed_recipes(recs) or recs

    if not recs:
        st.warning("候補が作成できませんでした。条件を見直してください。")
        st.stop()

    for rec in recs:
        rec.servings = servings
        rec.ingredients = normalize_ingredients(rec.ingredients, rec.servings, child_mode=child_mode, child_factor=0.8 if child_mode else 1.0)
        tools = rec.equipment or infer_tools_from_recipe(rec)
        est_cost = estimate_cost_yen(rec, price_factor=price_factor)
        nutri = estimate_nutrition(rec)
        score = score_against_profile(nutri, nutri_profile)

        st.divider()
        title_line = rec.recipe_title + ("　👨‍👩‍👧 子ども配慮" if child_mode else "")
        st.subheader(title_line)
        ok, _ = quality_check(rec)
        if ok: st.success("✅ 一般的な家庭料理として妥当な品質です")

        meta = []
        meta.append(f"**人数:** {rec.servings}人分")
        if rec.total_time_min:
            meta.append(f"**目安:** {rec.total_time_min}分")
        if rec.difficulty:
            meta.append(f"**難易度:** {rec.difficulty}")
        meta.append(f"**概算コスト:** 約 {est_cost} 円")
        st.markdown(" / ".join(meta))
        st.markdown("**器具:** " + ("、".join(tools) if tools else "特になし"))

        col_n1, col_n2 = st.columns([1,2])
        with col_n1:
            st.markdown("**栄養の概算（1人前）**")
            st.write(
                f"- エネルギー: {nutri['kcal']} kcal（{score['kcal']}）\n"
                f"- たんぱく質: {nutri['protein_g']} g（{score['protein_g']}）\n"
                f"- 脂質: {nutri['fat_g']} g\n"
                f"- 炭水化物: {nutri['carb_g']} g\n"
                f"- 塩分: {nutri['salt_g']} g（{score['salt_g']}）"
            )
        with col_n2:
            tips = []
            if child_mode:
                tips += ["辛味は後がけ/別添に（大人だけ七味やラー油）",
                         "根菜はレンジ下茹ででやわらかく（600W 2分）",
                         "酒はよく加熱してアルコールを飛ばす"]
            st.info("**ひと工夫**\n- " + "\n- ".join(tips) if tips else "—")

        st.markdown("**材料**")
        for i in rec.ingredients:
            base, qty_in_name = split_quantity_from_name(i.name)
            amt = sanitize_amount(getattr(i, "amount", None)) or qty_in_name or "適量"
            st.markdown(f"- {base} {amt}" + ("（任意）" if i.is_optional else "") + (f" / 代替: {i.substitution}" if i.substitution else ""))

        st.markdown("**手順**")
        for idx, s in enumerate(rec.steps, 1):
            line = strip_step_prefix(s.text)
            if child_mode:
                if any(k in line for k in SPICY_WORDS):
                    line += "（子ども向けは入れず、大人分に後から加える）"
                if "酒" in line and "加熱" not in line:
                    line += "（よく加熱してアルコールを飛ばす）"
            st.markdown(f"**STEP {idx}**　{line}")

    st.stop()

# -------- ここから 1週間モード --------
week_budget = int(st.session_state.get("week_budget", 8000))
num_days = int(st.session_state.get("week_days", 5))
prefer_cheap = bool(st.session_state.get("prefer_cheap", False))

with st.spinner("1週間の献立を作成中…"):
    plans, total_cost, feasible_budget, week_summary = plan_week(
        num_days=num_days, budget_yen=week_budget, servings=servings,
        theme=theme, genre=genre, max_minutes=max_minutes,
        price_factor=price_factor, child_mode=child_mode,
        want_keyword=want_keyword, avoid_keywords=avoid_keywords,
        profile_name=nutri_profile, prefer_cheap=prefer_cheap
    )

# 予算サマリ
if feasible_budget is not None and feasible_budget > week_budget:
    st.warning(f"⚠️ 入力した予算 {week_budget:,} 円では実現が難しいため、"
               f"**少なくとも {feasible_budget:,} 円** 程度が必要です（概算・地域係数 {price_factor:.2f}）。")
else:
    st.success(f"✅ 予算内に収まりました：合計 **{total_cost:,} 円** / 予算 {week_budget:,} 円（概算・地域係数 {price_factor:.2f}）")

# 週の栄養スコア
avg = week_summary["avg"]; sc = week_summary["score"]
st.subheader("🥗 週の栄養スコア（1日平均）")
st.write(
    f"- エネルギー: {avg['kcal']} kcal（{sc['kcal']}）\n"
    f"- たんぱく質: {avg['protein_g']} g（{sc['protein_g']}）\n"
    f"- 塩分: {avg['salt_g']} g（{sc['salt_g']}）"
)

# 日別カード
for p in sorted(plans, key=lambda x: x.day_index):
    rec = p.recipe
    tools = rec.equipment or infer_tools_from_recipe(rec)
    st.divider()
    st.subheader(f"Day {p.day_index}：{rec.recipe_title}" + ("　👨‍👩‍👧" if child_mode else ""))
    meta = []
    meta.append(f"**人数:** {rec.servings}人分")
    if rec.total_time_min: meta.append(f"**目安:** {rec.total_time_min}分")
    if rec.difficulty: meta.append(f"**難易度:** {rec.difficulty}")
    meta.append(f"**概算コスト:** 約 {p.est_cost} 円")
    st.markdown(" / ".join(meta))
    st.markdown("**器具:** " + ("、".join(tools) if tools else "特になし"))

    with st.expander("材料・手順を開く"):
        st.markdown("**材料**")
        for i in rec.ingredients:
            base, qty_in_name = split_quantity_from_name(i.name)
            amt = sanitize_amount(getattr(i, "amount", None)) or qty_in_name or "適量"
            st.markdown(f"- {base} {amt}" + ("（任意）" if i.is_optional else "") + (f" / 代替: {i.substitution}" if i.substitution else ""))
        st.markdown("**手順**")
        for idx, s in enumerate(rec.steps, 1):
            line = strip_step_prefix(s.text)
            if child_mode:
                if any(k in line for k in SPICY_WORDS):
                    line += "（子ども向けは入れず、大人分に後から加える）"
                if "酒" in line and "加熱" not in line:
                    line += "（よく加熱してアルコールを飛ばす）"
            st.markdown(f"**STEP {idx}**　{line}")

# 買い物リスト（合算）
def aggregate_shopping(plans: List[DayPlan]) -> Dict[str, Tuple[str,float]]:
    """name -> (unit, total_value) / unitは g/tbsp/tsp/piece のいずれか"""
    agg: Dict[str, Tuple[str, float]] = {}
    for p in plans:
        for ing in p.recipe.ingredients:
            name = ing.name
            unit, val = amount_to_grams_or_spoons(ing.amount or "")
            if unit == "": continue
            u, v = agg.get(name, (unit, 0.0))
            if u == unit:
                agg[name] = (u, v + val)
            else:
                # 単位が異なる場合は簡易換算
                gram_equiv = 0.0
                def to_g(u0, x):
                    if u0 == "g": return x
                    if u0 == "tbsp": return x*15
                    if u0 == "tsp": return x*5
                    if u0 == "piece": return x*50
                    return 0
                gram_equiv = to_g(u, v) + to_g(unit, val)
                agg[name] = ("g", gram_equiv)
    return agg

def pretty_amount(u: str, x: float) -> str:
    if u == "g": return _grams_to_pretty(int(round(x)))
    if u == "tbsp": return f"大さじ{round(x*2)/2:g}"
    if u == "tsp": return f"小さじ{round(x*2)/2:g}"
    if u == "piece":
        return f"{int(x) if abs(x-int(x))<1e-6 else x:g}個"
    return "適量"

agg = aggregate_shopping(plans)
st.subheader("🛒 1週間の買い物リスト（概算・合算）")
if not agg:
    st.write("—")
else:
    # 簡易カテゴリ分け
    CATS = {
        "精肉/魚": ["鶏","豚","牛","鮭","さば","ひき肉","ベーコン","ハム","ツナ","卵"],
        "青果":    ["玉ねぎ","ねぎ","長ねぎ","キャベツ","にんじん","じゃがいも","なす","ピーマン","もやし","ブロッコリー","きのこ","しめじ","えのき","トマト","小松菜","ほうれん草","青菜"],
        "調味料":  ["塩","砂糖","しょうゆ","醤油","みりん","酒","味噌","酢","ごま油","オリーブオイル","バター","顆粒だし","だし"],
        "その他":  []
    }
    def cat_of(nm:str)->str:
        for c, keys in CATS.items():
            if any(k in nm for k in keys):
                return c
        return "その他"

    by_cat: Dict[str, List[Tuple[str,str]]] = {"精肉/魚":[], "青果":[], "調味料":[], "その他":[]}
    for name,(u,x) in sorted(agg.items()):
        by_cat[cat_of(name)].append((name, pretty_amount(u,x)))

    for cat in ["精肉/魚","青果","調味料","その他"]:
        items = by_cat[cat]
        if not items: continue
        st.markdown(f"**{cat}**")
        for name, qty in items:
            st.markdown(f"- {name}: {qty}")

# 免責
st.caption("※ 価格と栄養はあくまで概算です。地域・季節・銘柄により±20%以上の差が出ることがあります。")
