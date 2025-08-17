# -*- coding: utf-8 -*-
# ごはんの神様に相談だ！ / Streamlit App（信頼DB照合・安全弁つき）
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
    "ENABLE_QUALITY_FILTER": True,
    "ENABLE_TRUST_DB_SAFETY": True,     # ★ 信頼DBでの補強を有効化
    "SHOW_DEBUG_PANEL": IS_DEV,
    "TEMPERATURE": 0.4 if not IS_DEV else 0.6,
    "WEEK_REPLAN_ATTEMPTS": 2,
}

# ============================================================
# モデル
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

class DayPlan(BaseModel):
    day_index: int
    recipe: Recipe
    est_cost: int

# ============================================================
# 正規化ユーティリティ
# ============================================================
_STEP_PREFIX_RE = re.compile(r"^\s*(?:STEP\s*\d+[:：\-\s]*|\d+[\.．、\)）]\s*|[①-⑳]\s*)")
def strip_step_prefix(text: str) -> str:
    return _STEP_PREFIX_RE.sub('', text or '').strip()

TSP_IN_TBSP = 3.0
_num_re = re.compile(r'([0-9]+(?:\.[0-9]+)?)')
def _has_number(s: str) -> bool: return bool(_num_re.search(s or ""))

def _round_tsp_to_pretty(tsp: float) -> str:
    if tsp <= 0.15: return "少々"
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
    return f"{int(round(g/step)*step)}g"

def sanitize_amount(amount: Optional[str]) -> Optional[str]:
    if not amount: return None
    a = amount.strip().replace("．", ".").replace(".0", "")
    if a in {"小さじ0","大さじ0","0g","0個","0片","0枚","0本","0cc","0ml"}: return "少々"
    return a

# 材料名の中に埋まった分量を抽出
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

# 既定量（材料名から推定）
PROTEIN_G_PER_SERV = {"鶏むね肉":100,"鶏もも肉":100,"豚肉":100,"牛肉":100,"ひき肉":100,"鮭":90,"さば":90,"豆腐":150,"木綿豆腐":150,"絹ごし豆腐":150,"卵":50}
VEG_G_PER_SERV = {"玉ねぎ":50,"ねぎ":10,"長ねぎ":20,"キャベツ":80,"にんじん":40,"じゃがいも":80,"なす":60,"ピーマン":40,"もやし":100,"ブロッコリー":70,"きのこ":60,"しめじ":60,"えのき":60,"トマト":80,"小松菜":70,"ほうれん草":70}
COND_TSP_PER_SERV = {"塩":0.125,"砂糖":0.5,"しょうゆ":1.0,"醤油":1.0,"みりん":1.0,"酒":1.0,"酢":1.0,"コチュジャン":0.5,"味噌":1.5,"顆粒だし":0.5}
OIL_TSP_PER_SERV = {"サラダ油":1.0,"ごま油":0.5,"オリーブオイル":1.0}
PIECE_PER_SERV = {"卵":"1個","にんにく":"0.5片","生姜":"0.5片"}
SPICY_WORDS = ["一味","七味","豆板醤","コチュジャン","ラー油","唐辛子","粉唐辛子"]

def _guess_amount(name: str, servings: int) -> str:
    for key, per in PIECE_PER_SERV.items():
        if key in name:
            m = _num_re.search(per); num = float(m.group(1)) if m else 1.0
            unit = per.replace(str(num).rstrip('0').rstrip('.'), '')
            total = num * servings
            return f"{int(total) if abs(total-int(total))<1e-6 else total:g}{unit}"
    for key, g in PROTEIN_G_PER_SERV.items():
        if key in name: return _grams_to_pretty(int(g*servings))
    for key, g in VEG_G_PER_SERV.items():
        if key in name: return _grams_to_pretty(int(g*servings))
    for key, tsp in OIL_TSP_PER_SERV.items():
        if key in name: return _round_tsp_to_pretty(tsp*servings)
    for key, tsp in COND_TSP_PER_SERV.items():
        if key in name: return _round_tsp_to_pretty(tsp*servings)
    if any(k in name for k in ["胡椒","こしょう","黒胡椒","一味","七味","ラー油"]): return "少々"
    return "適量"

def normalize_ingredients(ings: List[Ingredient], servings: int, child_mode: bool=False, child_factor: float=0.8) -> List[Ingredient]:
    def is_condiment(nm:str)->bool:
        KEYS=["塩","砂糖","しょうゆ","醤油","みりん","酒","味噌","酢","ごま油","オリーブオイル","油","バター","だし","顆粒だし","コンソメ","ブイヨン"]
        return any(k in nm for k in KEYS)
    def is_spicy(nm:str)->bool:
        return any(k in nm for k in SPICY_WORDS)

    fixed: List[Ingredient] = []
    for it in ings:
        base_name, qty_in_name = split_quantity_from_name(it.name)
        amt = sanitize_amount(getattr(it, "amount", None)) or qty_in_name or ""
        if (not amt) or ("適量" in amt) or (not _has_number(amt) and "少々" not in amt):
            amt = _guess_amount(base_name, servings)
        amt = sanitize_amount(amt) or "適量"

        if child_mode:
            # 辛味は後がけ、調味は-20%
            if is_spicy(base_name): amt = "少々（子どもは後がけ）"
            if is_condiment(base_name):
                # 小さじ/大さじ/g に限って減らす
                def to_unit_val(a:str)->tuple[str,float]:
                    a=a.replace("．",".")
                    m=re.search(r'大さじ\s*(\d+(?:\.\d+)?)',a);   # tbsp
                    if m: return ("tbsp", float(m.group(1)))
                    m=re.search(r'小さじ\s*(\d+(?:\.\d+)?)',a);   # tsp
                    if m: return ("tsp", float(m.group(1)))
                    m=re.search(r'(\d+(?:\.\d+)?)\s*g',a);        # g
                    if m: return ("g", float(m.group(1)))
                    return ("",0.0)
                def from_unit_val(u,v)->str:
                    if u=="tbsp": return f"大さじ{round(v*2)/2:g}" if v>0 else "少々"
                    if u=="tsp":  return f"小さじ{round(v*2)/2:g}" if v>0 else "少々"
                    if u=="g":    return _grams_to_pretty(int(round(v))) if v>0 else "少々"
                    return amt
                u,v = to_unit_val(amt); 
                if v>0: amt = from_unit_val(u, v*child_factor)

        fixed.append(Ingredient(name=base_name, amount=amt,
                                is_optional=getattr(it,"is_optional",False),
                                substitution=getattr(it,"substitution",None)))
    return fixed

# ============================================================
# 量のパース（g, ml, tbsp, tsp, 個）
# ============================================================
def amount_to_unit_val(amount: str) -> tuple[str, float]:
    if not amount: return ("", 0.0)
    a = amount.replace("．",".").strip().lower()
    m = re.search(r'大さじ\s*(\d+(?:\.\d+)?)', a)
    if m: return ("tbsp", float(m.group(1)))
    m = re.search(r'小さじ\s*(\d+(?:\.\d+)?)', a)
    if m: return ("tsp", float(m.group(1)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:ml|mL|cc)', a)
    if m: return ("ml", float(m.group(1)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:g|グラム)', a)
    if m: return ("g", float(m.group(1)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*カップ', a)
    if m: return ("ml", float(m.group(1))*200.0)  # 日本の計量カップ200ml前提
    m = re.search(r'(\d+(?:\.\d+)?)\s*個', a)
    if m: return ("piece", float(m.group(1)))
    return ("", 0.0)

def unit_val_to_amount(u: str, v: float) -> str:
    if u=="tbsp":
        v = round(v*2)/2
        return f"大さじ{v:g}" if v>0 else "少々"
    if u=="tsp":
        v = round(v*2)/2
        return f"小さじ{v:g}" if v>0 else "少々"
    if u=="ml":
        return f"{int(round(v))}ml" if v>0 else "少々"
    if u=="g":
        return _grams_to_pretty(int(round(v))) if v>0 else "少々"
    if u=="piece":
        return f"{int(v) if abs(v-int(v))<1e-6 else v:g}個" if v>0 else "少々"
    return sanitize_amount(str(v)) or "適量"

# ============================================================
# 簡易 栄養/価格テーブル（概算）
# ============================================================
NUTRI_PROFILES = {
    "ふつう":   {"kcal": (500, 800), "protein_g": (20, 35), "salt_g": (0, 2.5)},
    "ダイエット":{"kcal": (350, 600), "protein_g": (25, 40), "salt_g": (0, 2.0)},
    "がっつり": {"kcal": (700,1000), "protein_g": (35, 55), "salt_g": (0, 3.0)},
    "減塩":     {"kcal": (500, 800), "protein_g": (20, 35), "salt_g": (0, 2.0)},
}

FOODS = {
    # 固形（100g基準）
    "鶏むね肉":{"kcal":120,"protein_g":23,"fat_g":2,"carb_g":0,"salt_g":0,"yen_per_100g":68},
    "鶏もも肉":{"kcal":200,"protein_g":17,"fat_g":14,"carb_g":0,"salt_g":0,"yen_per_100g":98},
    "豚肉":{"kcal":242,"protein_g":20,"fat_g":19,"carb_g":0,"salt_g":0,"yen_per_100g":128},
    "玉ねぎ":{"kcal":37,"protein_g":1,"fat_g":0,"carb_g":9,"salt_g":0,"yen_per_piece":40,"piece_g":180},
    "キャベツ":{"kcal":23,"protein_g":1,"fat_g":0,"carb_g":5,"salt_g":0,"yen_per_100g":25},
    "にんじん":{"kcal":37,"protein_g":1,"fat_g":0,"carb_g":9,"salt_g":0,"yen_per_100g":28},
    "ピーマン":{"kcal":22,"protein_g":1,"fat_g":0,"carb_g":5,"salt_g":0,"yen_per_100g":40},
    "木綿豆腐":{"kcal":72,"protein_g":7,"fat_g":4,"carb_g":2,"salt_g":0,"yen_per_piece":62,"piece_g":300},
    "卵":{"kcal":150,"protein_g":12,"fat_g":10,"carb_g":0,"salt_g":0,"yen_per_piece":25,"piece_g":50},
    # 液体（100ml基準）
    "生クリーム":{"kcal":330,"protein_g":2.0,"fat_g":35,"carb_g":3,"salt_g":0.1,"yen_per_100ml":120},
    "牛乳":{"kcal":67,"protein_g":3.4,"fat_g":3.8,"carb_g":5,"salt_g":0.1,"yen_per_100ml":25},
    # 調味（大さじ基準）
    "塩":{"kcal":0,"protein_g":0,"fat_g":0,"carb_g":0,"salt_g":6.0,"yen_per_tsp":2},
    "コンソメ":{"kcal":12,"protein_g":0.6,"fat_g":0.4,"carb_g":1.5,"salt_g":2.5,"yen_per_tsp":8},
    "しょうゆ":{"kcal":13,"protein_g":1.4,"fat_g":0,"carb_g":1.2,"salt_g":2.6,"yen_per_tbsp":10},
    "オリーブオイル":{"kcal":111,"protein_g":0,"fat_g":12.6,"carb_g":0,"salt_g":0,"yen_per_tbsp":20},
}

def tbsp_from_tsp(x: float) -> float: return x/3.0

def estimate_nutrition(rec) -> dict:
    total = {"kcal":0.0,"protein_g":0.0,"fat_g":0.0,"carb_g":0.0,"salt_g":0.0}
    for ing in rec.ingredients:
        name = ing.name; amt_str = ing.amount or ""
        unit, val = amount_to_unit_val(amt_str)
        key=None
        for k in FOODS.keys():
            if k in name: key=k; break
        if not key: continue
        base = FOODS[key].copy()
        factor=0.0
        if unit=="g": factor = val/100.0
        elif unit=="ml": factor = val/100.0
        elif unit=="tbsp":
            if "yen_per_tbsp" in base: factor = val
            else: factor = (val*15.0)/100.0
        elif unit=="tsp":
            if key in ["塩","コンソメ"]: factor = val
            elif "yen_per_tbsp" in base: factor = tbsp_from_tsp(val)
            else: factor = (val*5.0)/100.0
        elif unit=="piece":
            piece_g = base.get("piece_g",50)
            factor = (piece_g*val)/100.0
        for k in total: total[k]+= base[k]*factor
    serv = max(1, getattr(rec,"servings",1))
    for k in total: total[k] = round(total[k]/serv,1)
    return total

def score_against_profile(nutri: dict, profile_name: str) -> dict:
    prof = NUTRI_PROFILES.get(profile_name, NUTRI_PROFILES["ふつう"])
    def mark(val, rng):
        lo, hi = rng
        if val < lo*0.9: return "△"
        if lo <= val <= hi: return "◎"
        if val <= hi*1.15: return "△"
        return "⚠"
    return {"kcal":mark(nutri["kcal"],prof["kcal"]),
            "protein_g":mark(nutri["protein_g"],prof["protein_g"]),
            "salt_g":mark(nutri["salt_g"],prof["salt_g"])}

def estimate_cost_yen(rec: Recipe, price_factor: float = 1.0) -> int:
    total = 0.0
    for ing in rec.ingredients:
        name = ing.name; amt = ing.amount or ""
        unit, val = amount_to_unit_val(amt)
        key=None
        for k in FOODS.keys():
            if k in name: key=k; break
        if not key:
            if unit=="g": total += (val/100.0)*30*price_factor
            elif unit=="ml": total += (val/100.0)*20*price_factor
            continue
        meta = FOODS[key]
        if "yen_per_piece" in meta:
            if unit=="piece": total += meta["yen_per_piece"]*val*price_factor
            elif unit=="g":
                piece_g = meta.get("piece_g",100)
                pieces = val/piece_g
                total += meta["yen_per_piece"]*pieces*price_factor
            else:
                total += meta["yen_per_piece"]*price_factor
        if "yen_per_100g" in meta:
            grams = val
            if unit=="tbsp": grams = 15*val
            elif unit=="tsp": grams = 5*val
            elif unit=="piece": grams = meta.get("piece_g",50)*val
            elif unit=="g": grams = val
            total += (grams/100.0)*meta["yen_per_100g"]*price_factor
        if "yen_per_100ml" in meta:
            ml = val
            if unit=="tbsp": ml = 15*val
            elif unit=="tsp": ml = 5*val
            elif unit=="ml": ml = val
            elif unit=="g": ml = val # 近似
            total += (ml/100.0)*meta["yen_per_100ml"]*price_factor
        if "yen_per_tbsp" in meta:
            if unit=="tbsp": total += meta["yen_per_tbsp"]*val*price_factor
            elif unit=="tsp": total += meta["yen_per_tbsp"]*tbsp_from_tsp(val)*price_factor
            else: total += meta["yen_per_tbsp"]*price_factor
    return int(round(total))

# ============================================================
# 品質チェック（簡易）
# ============================================================
HEAT_WORDS = ["弱火","中火","強火","沸騰","余熱","レンジ","600W","500W"]
SEASONINGS = ["塩","砂糖","しょうゆ","醤油","みりん","酒","味噌","酢","ごま油","オリーブオイル","バター","だし","顆粒だし","コンソメ","ブイヨン"]
def quality_check(rec) -> tuple[bool, List[str]]:
    warns=[]
    if len(rec.ingredients)<3: warns.append("材料が少なすぎます（3品以上推奨）")
    if len(rec.steps)<3: warns.append("手順が少なすぎます（3ステップ以上推奨）")
    step_text="。".join([s.text for s in rec.steps])
    if not any(w in step_text for w in HEAT_WORDS):
        warns.append("加熱の記述がありません（弱火/中火/強火/レンジ）")
    ing_txt="、".join([f"{i.name} {i.amount or ''}" for i in rec.ingredients])
    if not any(s in ing_txt for s in SEASONINGS):
        warns.append("基本調味が不足（塩・しょうゆ等）")
    if "適量" in ing_txt:
        warns.append("“適量”が含まれています（できるだけ数量表記に）")
    return (len(warns)==0), warns

# ============================================================
# 信頼DB（基準ルール） & 照合・補強
# ============================================================
TRUST_DB = {
    "cream_stew": {
        "aliases": ["クリーム煮","クリームシチュー","鶏肉とキャベツのクリーム煮"],
        "min_sauce_ml_per_serv": 120,              # ソース量の下限（ml/人）
        "require_one_of_seasonings": ["コンソメ","ブイヨン","鶏がらスープ"],
        "root_veg_prep": {
            "にんじん": {"method":"薄めに切る＋レンジ600W 2-3分 もしくは 煮込み15分以上"},
            "じゃがいも": {"method":"大きめはレンジ下ごしらえ 2-3分 もしくは 煮込み15分以上"}
        },
        "pot_guideline": [
            {"min_total_g": 0,   "pot": "22-24cm フライパン/鍋"},
            {"min_total_g": 700, "pot": "24-26cm 深めフライパン/鍋"}
        ]
    }
}

def _match_trust_key(rec: Recipe) -> Optional[str]:
    title = rec.recipe_title
    for key, spec in TRUST_DB.items():
        if any(alias in title for alias in spec["aliases"]):
            return key
    # タイトルでヒットしなくても材料から推測（乳製品+煮込み）
    ing_names = " ".join([i.name for i in rec.ingredients])
    if ("生クリーム" in ing_names or "牛乳" in ing_names) and ("煮" in title or "シチュー" in title):
        return "cream_stew"
    return None

def _sum_sauce_ml(rec: Recipe) -> float:
    total_ml=0.0
    for i in rec.ingredients:
        if any(k in i.name for k in ["生クリーム","牛乳"]):
            u,v = amount_to_unit_val(i.amount or "")
            if u=="ml": total_ml+=v
            elif u=="tbsp": total_ml+= 15*v
            elif u=="tsp": total_ml+= 5*v
            elif u=="g": total_ml+= v # 近似
            elif u=="piece": total_ml+= 200*v # 近似（パック扱い）
    return total_ml

def _total_rough_weight_g(rec: Recipe) -> int:
    total=0.0
    for i in rec.ingredients:
        u,v = amount_to_unit_val(i.amount or "")
        if u=="g": total+=v
        elif u=="ml": total+=v*1.0
        elif u=="tbsp": total+=15*v
        elif u=="tsp": total+=5*v
        elif u=="piece":
            # 代表値
            if "玉ねぎ" in i.name: total+=180*v
            elif "卵" in i.name: total+=50*v
            else: total+=50*v
    return int(round(total))

def apply_trust_safety(rec: Recipe) -> tuple[Recipe, List[str], List[str]]:
    """
    return: (補強後レシピ, バッジ, 補強メモ)
    """
    badges=[]; notes=[]
    tk = _match_trust_key(rec)
    if not tk: return rec, badges, notes
    spec = TRUST_DB[tk]

    # 1) ソース量（ml/人）チェック
    need_per_serv = spec["min_sauce_ml_per_serv"]
    have_ml = _sum_sauce_ml(rec)
    min_total_need = need_per_serv * max(1, rec.servings)
    if have_ml < min_total_need:
        add_ml = min_total_need - have_ml
        # 既存の乳製品に加算 or 追加
        target = None
        for i in rec.ingredients:
            if "生クリーム" in i.name or "牛乳" in i.name:
                target = i; break
        if target is None:
            # 生クリームがなければ牛乳で追加
            rec.ingredients.append(Ingredient(name="牛乳", amount=f"{int(round(add_ml))}ml", substitution="生クリーム"))
        else:
            u,v = amount_to_unit_val(target.amount or "")
            if u=="": u="ml"; v=0.0
            if u!="ml":
                # なるべくmlベースに寄せる
                if u=="tbsp": v = 15*v
                elif u=="tsp": v = 5*v
                elif u=="g": v = v
                elif u=="piece": v = v*200
                u="ml"
            target.amount = unit_val_to_amount("ml", v + add_ml)
        badges.append("ソース量を基準化")
        notes.append(f"ソースが少なめだったため、{int(round(add_ml))}ml 追加しました（目安 {need_per_serv}ml/人）。")

    # 2) 味の芯（コンソメ/ブイヨン等）
    need_one = spec["require_one_of_seasonings"]
    ing_text = " ".join([i.name for i in rec.ingredients])
    if not any(k in ing_text for k in need_one):
        tsp = max(1.0, math.ceil(rec.servings/2))  # 2人で小さじ1目安
        rec.ingredients.append(Ingredient(name="コンソメ", amount=f"小さじ{tsp:g}"))
        badges.append("味の芯を補強")
        notes.append("風味の芯が弱かったため、コンソメを追加しました。")

    # 3) 根菜の下ごしらえ／煮込み時間
    root_spec = spec.get("root_veg_prep", {})
    root_hit = [nm for nm in root_spec.keys() if any(nm in i.name for i in rec.ingredients)]
    if root_hit:
        # 手順の先頭に下ごしらえを注入（重複回避）
        prep_sentence = []
        if "にんじん" in root_hit: prep_sentence.append("にんじんは薄めに切り、レンジ600Wで2〜3分下ごしらえする。")
        if "じゃがいも" in root_hit: prep_sentence.append("じゃがいもは大きければレンジ600Wで2〜3分下ごしらえする。")
        if prep_sentence:
            if not any("レンジ" in s.text and "下ごしらえ" in s.text for s in rec.steps):
                rec.steps.insert(0, Step(text=" ".join(prep_sentence)))
                badges.append("根菜の下ごしらえを追加")
                notes.append("根菜が固くなりにくいよう、レンジ下ごしらえを冒頭に追加しました。")
        # 煮込み時間が10分程度なら15分に引上げ表現へ（文言調整）
        for s in rec.steps:
            if ("煮" in s.text or "煮込" in s.text) and ("10分" in s.text) and ("弱火" in s.text or "中火" in s.text):
                s.text = s.text.replace("10分", "15分")
                badges.append("煮込み時間を補強")
                notes.append("にんじん等に火が入りやすいよう、煮込み目安を15分に調整しました。")
                break

    # 4) 鍋サイズガイド
    total_g = _total_rough_weight_g(rec)
    guide=""; last=""; 
    for gl in spec["pot_guideline"]:
        if total_g >= gl["min_total_g"]: last = gl["pot"]
    guide = last or spec["pot_guideline"][0]["pot"]
    if guide:
        if rec.equipment is None: rec.equipment=[]
        if not any("cm" in e or "フライパン" in e and "鍋" in e for e in rec.equipment):
            rec.equipment.append(guide)
            badges.append("鍋サイズを明示")
            notes.append(f"材料量から {guide} を推奨します。")

    # 5) ステップ末尾に味見＆調整を追加（なければ）
    if not any(("味を調える" in s.text) or ("味見" in s.text) for s in rec.steps):
        rec.steps.append(Step(text="味見をし、塩・胡椒で最終調整する。"))
        badges.append("味見・最終調整を明示")
        notes.append("味のばらつきを抑えるため、味見ステップを明示しました。")

    # 完了
    if badges:
        badges.insert(0, "信頼DBで補強済")
    return rec, badges, notes

# ============================================================
# OpenAI呼び出し（フォールバックあり）
# ============================================================
USE_OPENAI = True
try:
    from openai import OpenAI
    _client = OpenAI() if (USE_OPENAI and (os.getenv("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY"))) else None
except Exception:
    _client = None

PROMPT_TMPL = (
    "You are a helpful Japanese cooking assistant.\n"
    "Given inputs, propose 1–3 Japanese home recipes.\n"
    "Output strict JSON with schema:\n"
    "{ 'recommendations':[ { 'title':string,'servings':int,'total_time_min':int,'difficulty':string,"
    "'ingredients':[{'name':string,'amount':string|null,'is_optional':boolean,'substitution':string|null}],"
    "'steps':[{'text':string}],'equipment':string[]|null } ] }\n"
    "Avoid '適量' if possible; prefer g/ml/大さじ/小さじ. Include heat levels.\n"
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
) -> RecipeSet:
    avoid_keywords = avoid_keywords or []
    if _client is not None:
        try:
            theme_line = f"テーマ: {theme}\n" if theme else ""
            genre_line = f"ジャンル: {genre}\n" if genre else ""
            child_line = "子ども配慮: はい（辛味抜き・塩分-20%・一口大）\n" if child_mode else ""
            want_line  = ("希望: " + want_keyword) if want_keyword else "希望: なし"
            avoid_line = ("除外: " + ", ".join(avoid_keywords)) if avoid_keywords else "除外: なし"
            user_msg = (
                f"食材: {', '.join(ingredients) if ingredients else '（未指定）'}\n"
                f"人数: {servings}人\n"
                f"{theme_line}{genre_line}{child_line}"
                f"最大調理時間: {max_minutes}分\n"
                f"{want_line}\n{avoid_line}\n"
            )
            resp = _client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=FEATURES["TEMPERATURE"],
                messages=[{"role":"system","content":PROMPT_TMPL},
                          {"role":"user","content":user_msg}],
            )
            text = resp.choices[0].message.content or "{}"
            data = json.loads(text)
            return RecipeSet.model_validate(data)
        except Exception as e:
            st.info(f"LLMの構造化生成に失敗したためフォールバックします: {e}")

    # Fallback（最低1件）
    base_ings=[Ingredient(name=x) for x in ingredients[:6]] or [Ingredient(name="鶏もも肉"),Ingredient(name="キャベツ")]
    steps=[Step(text="材料を切る"),Step(text="フライパンで加熱し、調味する（中火）"),Step(text="味を整えて仕上げる")]
    rec=Recipe(title="鶏肉とキャベツのクリーム煮", servings=servings, total_time_min=min(30,max_minutes),
               difficulty="かんたん", ingredients=base_ings+[Ingredient(name="生クリーム",amount="200ml")], steps=steps)
    return RecipeSet(recommendations=[rec])

# ============================================================
# 週プラン（簡易）
# ============================================================
PROTEIN_ROTATION = ["鶏むね肉","豚肉","豆腐","鮭","鶏もも肉","卵","さば"]

def plan_week(num_days:int, budget_yen:int, servings:int, theme:str, genre:str, max_minutes:int,
              price_factor:float, child_mode:bool, want_keyword:str, avoid_keywords:List[str],
              nutri_profile:str) -> tuple[List[DayPlan], int]:
    plans=[]
    for i in range(num_days):
        hint = PROTEIN_ROTATION[i%len(PROTEIN_ROTATION)]
        data = generate_recipes([hint], servings, theme, genre, max_minutes, want_keyword, avoid_keywords, child_mode)
        recs = data.recommendations or []
        if FEATURES["ENABLE_QUALITY_FILTER"]:
            if want_keyword:
                matched=[r for r in recs if want_keyword.lower() in r.recipe_title.lower()]
                recs = matched + [r for r in recs if r not in matched]
        if not recs: continue
        r = recs[0]
        r.servings = servings
        r.ingredients = normalize_ingredients(r.ingredients, r.servings, child_mode)
        # ★ 信頼DBで補強
        if FEATURES["ENABLE_TRUST_DB_SAFETY"]:
            r, badges, notes = apply_trust_safety(r)
        est_cost = estimate_cost_yen(r, price_factor)
        plans.append(DayPlan(day_index=i+1, recipe=r, est_cost=est_cost))
    total_cost = sum(p.est_cost for p in plans)
    # 予算超過時は高コスト日を1回だけ再生成（軽量）
    if total_cost > budget_yen:
        plans.sort(key=lambda x:x.est_cost, reverse=True)
        if plans:
            data = generate_recipes(["豆腐"], servings, theme, genre, max_minutes, want_keyword, avoid_keywords, child_mode)
            if data.recommendations:
                r=data.recommendations[0]
                r.servings=servings; r.ingredients=normalize_ingredients(r.ingredients, r.servings, child_mode)
                if FEATURES["ENABLE_TRUST_DB_SAFETY"]:
                    r,_,_=apply_trust_safety(r)
                plans[0]=DayPlan(day_index=plans[0].day_index, recipe=r, est_cost=estimate_cost_yen(r, price_factor))
        total_cost = sum(p.est_cost for p in plans)
    return plans, total_cost

# ============================================================
# UI フォーム
# ============================================================
with st.form("inputs", clear_on_submit=False, border=True):
    mode = st.radio("提案範囲", ["1日分","1週間分"], horizontal=True)
    st.text_input("冷蔵庫の食材（カンマ区切り・任意）", key="ingredients", placeholder="例）鶏肉, キャベツ, 玉ねぎ")
    c1,c2,c3 = st.columns([1,1,1])
    with c1: st.slider("人数（合計）", 1, 8, 4, 1, key="servings")
    with c2:
        themes=["（お任せ）","時短","節約","栄養重視","子ども向け","おもてなし"]
        st.selectbox("テーマ", themes, index=0, key="theme")
    with c3:
        genres=["（お任せ）","和風","洋風","中華風","韓国風","エスニック"]
        st.selectbox("ジャンル", genres, index=0, key="genre")
    st.slider("最大調理時間（分）", 5, 90, 45, 5, key="max_minutes")
    st.text_input("作りたい料理名・キーワード（任意）", key="want_keyword", placeholder="例）クリーム煮、麻婆豆腐")
    st.text_input("除外したい料理名・キーワード（カンマ区切り・任意）", key="avoid_keywords", placeholder="例）揚げ物, 辛い")
    st.checkbox("子ども向け配慮（辛味抜き・塩分ひかえめ）", value=False, key="child_mode")
    st.selectbox("栄養目安プロファイル", list(NUTRI_PROFILES.keys()), index=0, key="nutri_profile")

    if mode=="1週間分":
        w1,w2 = st.columns([1,1])
        with w1: st.number_input("今週の予算（円）", min_value=1000, step=500, value=8000, key="week_budget")
        with w2:
            st.slider("今週つくる回数（外食日は除外）", 3, 7, 5, 1, key="week_days")
        st.select_slider("価格感（地域/体感係数）", options=["安め","ふつう","やや高め","高め"], value="ふつう", key="price_profile")

    st.checkbox("信頼DBで補強（推奨）", value=True, key="use_trust")
    submitted = st.form_submit_button("提案を作成", use_container_width=True)

if not submitted: st.stop()

# 入力整形
ing_text = st.session_state.get("ingredients","")
ingredients_raw = [s for s in (t.strip() for t in re.split(r"[、,]", ing_text)) if s]
theme = st.session_state.get("theme","");   theme = "" if theme=="（お任せ）" else theme
genre = st.session_state.get("genre","");   genre = "" if genre=="（お任せ）" else genre
servings = int(st.session_state.get("servings",4))
max_minutes = int(st.session_state.get("max_minutes",45))
want_keyword = (st.session_state.get("want_keyword") or "").strip()
avoid_keywords = [s for s in (t.strip() for t in re.split(r"[、,]", st.session_state.get("avoid_keywords") or "")) if s]
child_mode = bool(st.session_state.get("child_mode",False))
nutri_profile = st.session_state.get("nutri_profile","ふつう")
price_factor = {"安め":0.9,"ふつう":1.0,"やや高め":1.1,"高め":1.2}.get(st.session_state.get("price_profile","ふつう"),1.0)
FEATURES["ENABLE_TRUST_DB_SAFETY"] = bool(st.session_state.get("use_trust", True))

# ============================================================
# 分岐：1日 / 1週間
# ============================================================
if mode=="1日分":
    data = generate_recipes(ingredients_raw, servings, theme, genre, max_minutes, want_keyword, avoid_keywords, child_mode)
    recs = data.recommendations or []
    if not recs:
        st.warning("候補が作成できませんでした。条件を見直してください。"); st.stop()

    for rec in recs:
        rec.servings = servings
        rec.ingredients = normalize_ingredients(rec.ingredients, rec.servings, child_mode)
        # 安全弁：信頼DB補強
        badges=[]; notes=[]
        if FEATURES["ENABLE_TRUST_DB_SAFETY"]:
            rec, badges, notes = apply_trust_safety(rec)

        ok,_ = quality_check(rec)
        tools = rec.equipment or []
        est_cost = estimate_cost_yen(rec, price_factor)
        nutri = estimate_nutrition(rec)
        score = score_against_profile(nutri, nutri_profile)

        st.divider()
        st.subheader(rec.recipe_title + ("　👨‍👩‍👧" if child_mode else ""))
        meta=[]
        meta.append(f"**人数:** {rec.servings}人分")
        if rec.total_time_min: meta.append(f"**目安:** {rec.total_time_min}分")
        if rec.difficulty: meta.append(f"**難易度:** {rec.difficulty}")
        meta.append(f"**概算コスト:** 約 {est_cost} 円")
        st.markdown(" / ".join(meta))

        if ok: st.success("✅ 一般的な家庭料理として妥当な品質です")
        if badges:
            st.info("🛡 **信頼DBで補強**：" + " / ".join(badges))
            if notes:
                st.caption("補強内容:\n- " + "\n- ".join(notes))

        if tools: st.markdown("**器具:** " + "、".join(tools))

        col1,col2 = st.columns([1,2])
        with col1:
            st.markdown("**栄養の概算（1人前）**")
            st.write(
                f"- エネルギー: {nutri['kcal']} kcal（{score['kcal']}）\n"
                f"- たんぱく質: {nutri['protein_g']} g（{score['protein_g']}）\n"
                f"- 塩分: {nutri['salt_g']} g（{score['salt_g']}）"
            )
        with col2:
            st.markdown("**材料**")
            for i in rec.ingredients:
                base,_ = split_quantity_from_name(i.name)
                amt = sanitize_amount(getattr(i,"amount",None)) or "適量"
                st.markdown(f"- {base} {amt}" + ("（任意）" if i.is_optional else "") + (f" / 代替: {i.substitution}" if i.substitution else ""))

        st.markdown("**手順**")
        for idx, s in enumerate(rec.steps,1):
            st.markdown(f"**STEP {idx}**　{strip_step_prefix(s.text)}")

    st.caption("※ 価格と栄養は概算です（地域・季節で±20%以上の差が出ます）。")
    st.stop()

# ---- 1週間モード ----
week_budget = int(st.session_state.get("week_budget",8000))
num_days = int(st.session_state.get("week_days",5))

with st.spinner("1週間の献立を作成中…"):
    plans, total_cost = plan_week(num_days, week_budget, servings, theme, genre, max_minutes, price_factor, child_mode, want_keyword, avoid_keywords, nutri_profile)

if total_cost > week_budget:
    st.warning(f"⚠️ 予算超過：合計 {total_cost:,} 円 / 予算 {week_budget:,} 円")
    st.caption("※ 高コスト日は安価食材へ自動置換を試みましたが、なお超過しています。")
else:
    st.success(f"✅ 予算内に収まりました：合計 {total_cost:,} 円 / 予算 {week_budget:,} 円")

# 日別カード
for p in sorted(plans, key=lambda x:x.day_index):
    rec=p.recipe
    st.divider()
    st.subheader(f"Day {p.day_index}：{rec.recipe_title}")
    meta=[]
    meta.append(f"**人数:** {rec.servings}人分")
    if rec.total_time_min: meta.append(f"**目安:** {rec.total_time_min}分")
    if rec.difficulty: meta.append(f"**難易度:** {rec.difficulty}")
    meta.append(f"**概算コスト:** 約 {p.est_cost} 円")
    st.markdown(" / ".join(meta))
    if rec.equipment: st.markdown("**器具:** " + "、".join(rec.equipment))
    with st.expander("材料・手順を開く"):
        st.markdown("**材料**")
        for i in rec.ingredients:
            base,_ = split_quantity_from_name(i.name)
            amt = sanitize_amount(getattr(i,"amount",None)) or "適量"
            st.markdown(f"- {base} {amt}" + ("（任意）" if i.is_optional else "") + (f" / 代替: {i.substitution}" if i.substitution else ""))
        st.markdown("**手順**")
        for idx,s in enumerate(rec.steps,1):
            st.markdown(f"**STEP {idx}**　{strip_step_prefix(s.text)}")

st.caption("※ 価格と栄養は概算です（地域・季節で±20%以上の差が出ます）。")
