"""
Telegram Bot Notifier
──────────────────────
Kirim alert screening result ke Telegram menggunakan Bot API langsung (httpx).
Tidak butuh library telegram-bot — lebih ringan, zero dependency tambahan.

Format notifikasi dirancang untuk scalp cepat:
- Keputusan besar di atas (GAS IT / SKIP)
- CA langsung bisa di-copy
- Semua info yang dibutuhkan dalam 1 pesan
- Link DexScreener & instruksi paste ke Trojan
"""

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from solana_screener import SolanaTokenResult

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


async def send_alert(token_result: "SolanaTokenResult", bot_token: str, chat_id: str) -> bool:
    """
    Kirim notifikasi screening result ke Telegram.
    Returns True jika berhasil terkirim.
    """
    if not bot_token or not chat_id:
        logger.warning("Telegram credentials tidak dikonfigurasi")
        return False

    text = _format_message(token_result)
    url = _TELEGRAM_API.format(token=bot_token)

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if r.status_code == 200:
                return True
            logger.warning(f"Telegram send failed: {r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False


def _format_message(r) -> str:
    """
    Format pesan Telegram dengan info lengkap untuk keputusan scalp cepat.
    Menggunakan HTML formatting.
    """
    is_gas = r.decision == "GAS IT"

    # ── Header ────────────────────────────────────────────────────────────
    if is_gas:
        header = (
            "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"┃  <b>GAS IT — ${r.symbol}</b>\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        header = (
            "┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"┃  <b>SKIP — ${r.symbol}</b>\n"
            "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    # ── Token Info ────────────────────────────────────────────────────────
    age_str = _format_age(r.pair_age_seconds)
    mcap_str = _fmt_usd(r.market_cap_usd)
    liq_str = f"{r.liquidity_sol_est:.1f} SOL (${_fmt_usd(r.liquidity_usd)})"
    vol5m_str = _fmt_usd(r.volume_5m_usd) if r.volume_5m_usd else "—"

    token_lines = (
        f"\n<code>{r.mint}</code>\n"
        f"\n<b>Name:</b> {r.name} | <b>Age:</b> {age_str}"
        f"\n<b>MCap:</b> {mcap_str} | <b>Tier:</b> {r.mcap_tier}"
        f"\n<b>Liq:</b> {liq_str}"
        f"\n<b>Vol 5m:</b> {vol5m_str}"
    )

    # ── Filter Results ────────────────────────────────────────────────────
    def _status(flag: bool) -> str:
        return "FLAG" if flag else "OK"

    safety_detail = (
        f"Mint {'Revoked' if r.mint_authority_revoked else '<b>AKTIF</b>'} | "
        f"Freeze {'Revoked' if r.freeze_authority_revoked else '<b>AKTIF</b>'}"
    )

    ratio_str = f" | Ratio: {r.mcap_liq_ratio:.1f}x" if r.mcap_liq_ratio else ""
    bundle_str = f"{r.max_same_slot} holders same slot" if r.max_same_slot else "clean"

    filter_lines = (
        f"\n\n<b>━ Filters ━</b>"
        f"\n[{_status(r.safety_flag)}] Safety  — {safety_detail}"
        f"\n[{_status(r.network_flag)}] Network — {r.priority_fee_microlamports:,} microlamports"
        f"\n[{_status(r.wallet_flag)}] Wallets — {r.holder_count} holders | "
        f"Fresh: {r.fresh_wallet_count} ({r.fresh_wallet_ratio:.0%}) | "
        f"Avg age: {r.avg_wallet_age_days:.0f}d (std {r.wallet_age_std_days:.0f}d)"
        f"\n[{_status(r.holder_flag)}] Holders — Top1: {r.top1_holder_pct:.1f}% | "
        f"Top10: {r.top10_combined_pct:.1f}%"
        f"\n[{_status(r.entry_flag)}] Entry   — {r.mcap_tier}{ratio_str}"
        f"\n[{_status(r.bundle_flag)}] Bundle  — {bundle_str}"
    )

    # GMGN supplementary info
    gmgn_risk = getattr(r, "gmgn_rug_risk", "") or ""
    gmgn_sm = getattr(r, "gmgn_smart_money", 0) or 0
    if gmgn_risk or gmgn_sm:
        sm_str = f" | 🐋 Smart money: {gmgn_sm}" if gmgn_sm else ""
        filter_lines += f"\n<b>GMGN:</b> Risk={gmgn_risk}{sm_str}"

    # ── Flag count & decision ─────────────────────────────────────────────
    total = r.total_flags
    decision_line = (
        f"\n\n<b>Flags: {total}/5</b>  →  "
        + ("<b>GAS IT! Potensi scalp 60-70%</b>" if is_gas else "<b>SKIP. Terlalu berisiko.</b>")
    )

    # ── Reasons (jika ada flag) ───────────────────────────────────────────
    reason_lines = ""
    if r.flag_reasons:
        reasons_text = "\n".join(f"  • {reason}" for reason in r.flag_reasons[:5])
        reason_lines = f"\n\n<b>Kenapa flag:</b>\n{reasons_text}"

    # ── CTA ───────────────────────────────────────────────────────────────
    cta = ""
    if is_gas:
        dex_url = r.dex_pair_url or f"https://dexscreener.com/solana/{r.mint}"
        cta = (
            f"\n\n<a href='{dex_url}'>DexScreener</a>  "
            "— Copy CA ke Trojan, GAS!"
        )

    return header + token_lines + filter_lines + decision_line + reason_lines + cta


def _fmt_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value/1_000:.1f}K"
    return f"${value:.2f}"


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
