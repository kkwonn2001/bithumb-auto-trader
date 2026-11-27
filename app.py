import time
from datetime import datetime
import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

BASE = "https://api.bithumb.com"
START_CASH = 500_000
BUY_KRW = 100_000
MAX_POSITIONS = 2
TAKE_PROFIT = 0.012
STOP_LOSS = -0.008
DAILY_STOP = 10_000

st.set_page_config(page_title="Bithumb Auto Trader", page_icon="📈", layout="wide")

@st.cache_data(ttl=20)
def get_markets():
    r = requests.get(f"{BASE}/v1/market/all", params={"is_details": "false"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    return [x for x in data if x.get("market", "").startswith("KRW-")]

@st.cache_data(ttl=10)
def get_tickers(markets):
    if not markets:
        return []
    out = []
    # Keep request batches modest.
    for i in range(0, len(markets), 50):
        batch = markets[i:i+50]
        r = requests.get(f"{BASE}/v1/ticker", params={"markets": ",".join(batch)}, timeout=12)
        r.raise_for_status()
        out.extend(r.json())
    return out

@st.cache_data(ttl=45)
def get_candles(market):
    r = requests.get(
        f"{BASE}/v1/candles/minutes/5",
        params={"market": market, "count": 30},
        timeout=10
    )
    r.raise_for_status()
    return r.json()

def score_market(ticker):
    market = ticker["market"]
    try:
        candles = get_candles(market)
        if len(candles) < 22:
            return None
        # API returns newest first; reverse to chronological order.
        df = pd.DataFrame(candles[::-1])
        close = pd.to_numeric(df["trade_price"])
        volume = pd.to_numeric(df["candle_acc_trade_volume"])
        last = float(close.iloc[-1])
        ma5 = float(close.tail(5).mean())
        ma20 = float(close.tail(20).mean())
        avg_vol = float(volume.tail(20).iloc[:-1].mean()) if len(volume) > 2 else 0
        vol_ratio = float(volume.iloc[-1] / avg_vol) if avg_vol > 0 else 0
        change_5 = float((last / close.iloc[-2] - 1) * 100) if close.iloc[-2] else 0
        # Conservative filter: trend positive, volume expansion, avoid extreme single-candle spike.
        if ma5 <= ma20 or last <= ma5 or vol_ratio < 1.5 or change_5 > 2.5:
            return None
        score = (vol_ratio * 2) + max(change_5, 0) + ((ma5 / ma20 - 1) * 100)
        return {
            "market": market,
            "price": last,
            "score": score,
            "volume_ratio": vol_ratio,
            "change_5m_pct": change_5
        }
    except Exception:
        return None

def init_state():
    defaults = {
        "cash": START_CASH,
        "positions": {},
        "trades": [],
        "realized_pnl": 0.0,
        "day_start": datetime.now().date().isoformat(),
        "halted": False,
        "paper_mode": True
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

st.title("📈 Bithumb Auto Trader")
st.caption("초기 버전 · 실시간 시세 분석 + 모의매매 · 실제 주문 기능 없음")
st_autorefresh(interval=15_000, key="auto_refresh")

with st.sidebar:
    st.subheader("운용 설정")
    st.metric("모의 원금", f"{START_CASH:,.0f}원")
    st.metric("1회 매수 한도", f"{BUY_KRW:,.0f}원")
    st.metric("최대 보유", f"{MAX_POSITIONS}종목")
    st.metric("익절 / 손절", "+1.2% / -0.8%")
    st.metric("일일 손실 중단", f"-{DAILY_STOP:,.0f}원")
    st.warning("현재는 모의매매 전용입니다. 실거래 주문은 전송하지 않습니다.")
    if st.button("모의 계좌 초기화", use_container_width=True):
        for k in ["cash", "positions", "trades", "realized_pnl", "day_start", "halted"]:
            st.session_state.pop(k, None)
        st.rerun()
    if st.button("긴급 중단", type="primary", use_container_width=True):
        st.session_state.halted = True
        st.success("모의 자동매매가 중단되었습니다.")
    if st.button("모의매매 재개", use_container_width=True):
        st.session_state.halted = False
        st.rerun()

try:
    markets_data = get_markets()
    market_codes = [x["market"] for x in markets_data]
    tickers = get_tickers(market_codes)
except Exception as e:
    st.error(f"빗썸 공개 시세를 불러오지 못했습니다: {e}")
    st.stop()

ticker_map = {x["market"]: x for x in tickers}
krw_total = st.session_state.cash
unrealized = 0.0
for market, pos in st.session_state.positions.items():
    current = ticker_map.get(market, {}).get("trade_price", pos["avg_price"])
    unrealized += (float(current) - pos["avg_price"]) * pos["qty"]

c1, c2, c3, c4 = st.columns(4)
c1.metric("현금 잔액", f"{st.session_state.cash:,.0f}원")
c2.metric("보유 종목", f"{len(st.session_state.positions)} / {MAX_POSITIONS}")
c3.metric("실현 손익", f"{st.session_state.realized_pnl:,.0f}원")
c4.metric("평가손익", f"{unrealized:,.0f}원")

# Candidate ranking: highest KRW turnover first, then apply candle strategy.
ranked = sorted(
    tickers,
    key=lambda x: float(x.get("acc_trade_price_24h", 0) or 0),
    reverse=True
)[:30]

candidates = []
if not st.session_state.halted:
    for t in ranked:
        result = score_market(t)
        if result:
            candidates.append(result)
    candidates.sort(key=lambda x: x["score"], reverse=True)

# Simulated exits
for market, pos in list(st.session_state.positions.items()):
    price = float(ticker_map.get(market, {}).get("trade_price", pos["avg_price"]))
    pnl_pct = price / pos["avg_price"] - 1
    if pnl_pct >= TAKE_PROFIT or pnl_pct <= STOP_LOSS or st.session_state.halted:
        proceeds = price * pos["qty"]
        pnl = proceeds - pos["cost"]
        st.session_state.cash += proceeds
        st.session_state.realized_pnl += pnl
        st.session_state.trades.append({
            "시간": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "마켓": market, "구분": "모의매도", "가격": price,
            "손익": pnl, "수익률(%)": pnl_pct * 100
        })
        del st.session_state.positions[market]

# Simulated entries
if not st.session_state.halted:
    for cand in candidates:
        if len(st.session_state.positions) >= MAX_POSITIONS:
            break
        market = cand["market"]
        if market in st.session_state.positions or st.session_state.cash < BUY_KRW:
            continue
        price = cand["price"]
        qty = BUY_KRW / price
        st.session_state.cash -= BUY_KRW
        st.session_state.positions[market] = {
            "avg_price": price, "qty": qty, "cost": BUY_KRW,
            "bought_at": datetime.now().isoformat()
        }
        st.session_state.trades.append({
            "시간": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "마켓": market, "구분": "모의매수", "가격": price,
            "손익": 0, "수익률(%)": 0
        })

st.subheader("자동 선정 매수 후보")
if candidates:
    st.dataframe(pd.DataFrame(candidates)[
        ["market", "price", "score", "volume_ratio", "change_5m_pct"]
    ].rename(columns={
        "market": "코인", "price": "현재가", "score": "전략점수",
        "volume_ratio": "거래량배수", "change_5m_pct": "5분변동률(%)"
    }), use_container_width=True, hide_index=True)
else:
    st.info("현재 조건을 충족하는 매수 후보가 없습니다. 무리하게 매수하지 않습니다.")

st.subheader("모의 보유 종목")
rows = []
for market, pos in st.session_state.positions.items():
    price = float(ticker_map.get(market, {}).get("trade_price", pos["avg_price"]))
    pnl = (price - pos["avg_price"]) * pos["qty"]
    rows.append({
        "코인": market, "매수가": pos["avg_price"], "현재가": price,
        "평가손익": pnl, "수익률(%)": (price / pos["avg_price"] - 1) * 100
    })
if rows:
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    st.write("현재 모의 보유 종목이 없습니다.")

st.subheader("최근 거래 내역")
if st.session_state.trades:
    st.dataframe(pd.DataFrame(st.session_state.trades[::-1]).head(30),
                 use_container_width=True, hide_index=True)
else:
    st.write("아직 거래 내역이 없습니다.")

if st.session_state.realized_pnl <= -DAILY_STOP:
    st.session_state.halted = True
    st.error("일일 손실 제한에 도달하여 모의매매를 중단했습니다.")

st.caption(
    "주의: 이 전략은 검증되지 않은 실험용 규칙입니다. 모의매매 결과는 실제 체결, "
    "수수료, 호가 미끄러짐, 서버 중단을 완전히 반영하지 않습니다. "
    "페이지 새로고침/세션 종료 시 모의 계좌 데이터가 초기화될 수 있습니다."
)
time.sleep(0.2)
st.caption("화면은 약 15초마다 자동 갱신됩니다.")
