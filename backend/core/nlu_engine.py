"""Three-stage intent parsing; simple commands never require a model request."""
from __future__ import annotations

import asyncio
import collections
import collections.abc
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

for _attr in ("Mapping", "MutableMapping", "Sequence", "MutableSequence", "Set", "MutableSet", "Callable", "Iterable", "Iterator"):
    if not hasattr(collections, _attr) and hasattr(collections.abc, _attr):
        setattr(collections, _attr, getattr(collections.abc, _attr))

from backend.core.config_manager import config_manager
from backend.core.schemas import IntentType, NLUResult

logger = logging.getLogger(__name__)
_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+(?:个半)?|半)"
_DURATION = rf"(?P<number>{_NUMBER})\s*(?:个)?\s*(?P<unit>分钟|分|小时|钟头|天)"

_DDL_HINT_RE = re.compile(
    r"截止|最晚|最迟|DDL|作答|提交|上交|交作业|前完成|前交|题量|满分|测验|测试|练习|作业|实验|报告|论文|问卷|报名|申报|打卡",
    re.I,
)
_NOISE_KV_RE = re.compile(
    r"(?:题量|满分|总分|得分|分值|状态|完成状态|发布人|创建人|限时|允许尝试)\s*[:：]\s*[^\s，,；;\n]+",
    re.I,
)


def _sync_post_chat_completions(endpoint: str, api_key: str, payload: dict, timeout_sec: float) -> tuple[int, str]:
    """Use standard library urllib.request to avoid third-party httpx/httpx2 wrapper conflicts."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "ChronoSecretary/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            text = resp.read().decode("utf-8", errors="replace")
            return int(status), text
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        return int(exc.code), err_body


def _number(text: str) -> float:
    text = text.strip()
    if text in ("半", "半个"):
        return 0.5
    if text.endswith("个半") and len(text) > 2:
        return _number(text[:-2]) + 0.5
    if text.endswith("个") and len(text) > 1:
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        digits = dict(zip("零〇一二两三四五六七八九", [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
        total, current = 0, 0
        for char in text:
            if char in digits:
                current = digits[char]
            elif char in "十百":
                total += (current or 1) * (10 if char == "十" else 100)
                current = 0
            else:
                raise ValueError("Unsupported Chinese number")
        return float(total + current)


def _duration_to_minutes(num_str: str, unit: str) -> int:
    num_clean = num_str.strip()
    if num_clean in ("半", "半个") and unit in ("小时", "钟头", "天"):
        value = 0.5 * (60 if unit in ("小时", "钟头") else 1440)
    else:
        value = _number(num_clean) * {"分钟": 1, "分": 1, "小时": 60, "钟头": 60, "天": 1440}[unit]
    if not value.is_integer() or not 0 < value <= 5256000:
        raise ValueError("Duration must be a positive whole number of minutes")
    return int(value)


def _minutes(match: re.Match) -> int:
    return _duration_to_minutes(match.group("number"), match.group("unit"))


def _parse_cn_datetime(expr: str, now: datetime, default_date: datetime | None = None) -> datetime | None:
    """确定性解析常见中文日期时间表达式（如 '半个小时后'、'11-07 11:52'、'11月5日23:59'、'明天下午3点'）。"""
    s = expr.strip(" ，。！？!?；;、\t\r\n")
    if not s:
        return None

    # 0. 纯相对时长：如 "半个小时后"、"30分钟后"、"2小时后"、"3天后"
    m_rel_dur = re.fullmatch(rf"{_DURATION}\s*(?:之)?后", s)
    if m_rel_dur:
        try:
            mins = _minutes(m_rel_dur)
            return now + timedelta(minutes=mins)
        except ValueError:
            return None

    # 1. X天后的 HH:MM / X天后下午3点
    m_rel_day = re.match(
        rf"^(?P<d>{_NUMBER})\s*天(?:之)?后(?:的)?\s*(?P<rest>.*)$", s
    )
    base_date = (default_date or now).date()
    rest = s
    has_date_part = default_date is not None

    if m_rel_day:
        try:
            days = int(_number(m_rel_day.group("d")))
            base_date = (now + timedelta(days=days)).date()
            rest = m_rel_day.group("rest").strip()
            has_date_part = True
        except ValueError:
            return None
    else:
        # 2. 今天 / 明天 / 后天 / 大后天
        m_day_word = re.match(r"^(?P<w>今天|今日|明天|明日|后天|大后天)\s*(?P<rest>.*)$", s)
        if m_day_word:
            offset = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}[m_day_word.group("w")]
            base_date = (now + timedelta(days=offset)).date()
            rest = m_day_word.group("rest").strip()
            has_date_part = True
        else:
            # 3. (YYYY年/-/. )? M月D日/号 或 MM-DD / MM/DD / MM.DD
            m_md = re.match(
                r"^(?:(?P<y>\d{4})\s*[年/.-])?\s*(?P<m>\d{1,2})\s*(?:月|[-/.])\s*(?P<d>\d{1,2})\s*(?:日|号)?(?:\s+|\b)(?P<rest>.*)$",
                s,
            ) or re.match(
                r"^(?:(?P<y>\d{4})\s*[年/.-])?\s*(?P<m>\d{1,2})\s*(?:月\s*(?P<d>\d{1,2})\s*(?:日|号)?)(?P<rest>.*)$",
                s,
            )
            if m_md:
                year = int(m_md.group("y")) if m_md.group("y") else now.year
                month, day = int(m_md.group("m")), int(m_md.group("d"))
                try:
                    candidate = datetime(year, month, day, tzinfo=now.tzinfo).date()
                    # 仅当无年份且日期比当前早超过 60 天（如跨年 12 月提次年 1 月）才顺延到下一年；
                    # 避免 10-29 解析 "10-27 11:52至11-07 11:52" 时把刚过去的开始时间 10-27 误推到明年
                    if not m_md.group("y") and (now.date() - candidate).days > 60:
                        candidate = datetime(year + 1, month, day, tzinfo=now.tzinfo).date()
                    base_date = candidate
                    rest = m_md.group("rest").strip()
                    has_date_part = True
                except ValueError:
                    return None

    # 解析时刻部分 (如 23:59, 11:52:00, 下午3点半, 晚上8点, 17时30分)
    if not rest:
        if not has_date_part:
            return None
        return datetime(base_date.year, base_date.month, base_date.day, 23, 59, tzinfo=now.tzinfo)

    m_time = re.match(
        r"^(?P<meridiem>凌晨|早上|上午|中午|下午|傍晚|晚上|晚间)?\s*"
        r"(?P<h>\d{1,2}|[零〇一二两三四五六七八九十]+)\s*(?:[:：点时])\s*"
        r"(?:(?P<half>半)|(?P<min>\d{1,2}|[零〇一二两三四五六七八九十]+)\s*分?(?:\s*[:：]?\s*\d{1,2}\s*秒?)?)?$",
        rest,
    )
    if not m_time:
        return None
    try:
        hour = int(_number(m_time.group("h")))
        if m_time.group("half"):
            minute = 30
        elif m_time.group("min"):
            minute = int(_number(m_time.group("min")))
        else:
            minute = 0
    except ValueError:
        return None

    meridiem = m_time.group("meridiem") or ""
    if meridiem in ("下午", "傍晚", "晚上", "晚间") and hour < 12:
        hour += 12
    elif meridiem == "中午" and hour < 11:
        hour += 12
    elif not meridiem and not has_date_part and hour <= 12:
        if 1 <= hour <= 7:
            hour += 12

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    dt = datetime(base_date.year, base_date.month, base_date.day, hour, minute, tzinfo=now.tzinfo)
    if not has_date_part and dt <= now:
        dt += timedelta(days=1)
    return dt


_DT_PART_PATTERN = (
    r"(?:"
    r"(?:(?:\d{4}\s*[年/.-])?\d{1,2}\s*(?:月|[-/.])\s*\d{1,2}\s*(?:日|号)?|今天|今日|明天|明日|后天|大后天)"
    r"(?:\s*(?:凌晨|早上|上午|中午|下午|傍晚|晚上|晚间)?\s*\d{1,2}\s*[:：点时]\s*(?:半|\d{1,2}\s*分?(?:\s*[:：]\s*\d{1,2}\s*秒?)?)?)?"
    r"|"
    r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上|晚间)?\s*\d{1,2}\s*[:：点时]\s*(?:半|\d{1,2}\s*分?(?:\s*[:：]\s*\d{1,2}\s*秒?)?)"
    r")"
)

_TIME_RANGE_RE = re.compile(
    rf"(?P<label>(?:作答|提交|截止|开放|有效|考试|活动|会议|开始|结束|持续|起止)?(?:时间|日期|期限)\s*[:：]\s*)?"
    rf"(?P<start>{_DT_PART_PATTERN})\s*(?:至|到|~|～|—|–|--|-|to)\s*(?P<end>{_DT_PART_PATTERN})",
    re.I,
)


def _parse_quote_reply_time(text: str, now: datetime) -> datetime | None:
    """通用引用回复时间提取：支持 '截止时间是11-07 11:52'、'改到明天下午3点'、'11-07 11:52截止'、纯时间等。"""
    cleaned = text.strip(" ，。！？!?；;\t\r\n")
    # 去除常见口语前缀与后缀
    stripped = re.sub(
        r"^(?:请|帮我|麻烦)?(?:把|将)?(?:这个|该|此)?(?:任务|事项|日程|备忘|作业|DDL)?(?:的)?"
        r"(?:截止时间|开始时间|提醒时间|结束时间|时间|日期|DDL|截止)?"
        r"\s*(?:是|为|设为|设置为|定在|定为|改到|改为|改成|调整到|调整为|推到|推迟到|延后到|换到|更新为|变成|[:：])?\s*",
        "",
        cleaned,
        flags=re.I,
    )
    stripped = re.sub(
        r"\s*(?:前|之前)?(?:截止|完成|交|提交)?(?:吧|呀|呢|哦|哈|再提醒(?:我)?)?$",
        "",
        stripped,
        flags=re.I,
    ).strip(" ，。！？!?：:")
    dt = _parse_cn_datetime(stripped, now)
    if dt is not None:
        return dt

    # 若包含时间区间（如引用回复 "作答时间:10-27 11:52至11-07 11:52"），取区间结束时间作为新截止时间
    m_range = _TIME_RANGE_RE.search(cleaned)
    if m_range:
        s_dt = _parse_cn_datetime(m_range.group("start"), now)
        e_dt = _parse_cn_datetime(m_range.group("end"), now, default_date=s_dt)
        if e_dt is not None:
            return e_dt
    return None


def _parse_notice_or_time_range(text: str, now: datetime) -> NLUResult | None:
    """
    通用多行通知/作业卡片与时间区间解析器：
    - 自动识别 '<start> 至/到/~/- <end>' 时间范围（如 '课堂练习第五章\\n题量: 1 满分: 100 作答时间:10-27 11:52至11-07 11:52'）
    - 自动区分『长跨度/作业作答窗口 -> DDL (取结束时间 deadline)』与『短时会议/活动 -> 日程 (取 start_time + end_time)』
    - 自动清洗题量/满分等元信息噪音，提取干净标题并保留详情到 description
    """
    body_text, adv_mins = _extract_advance_minutes(text)
    m_range = _TIME_RANGE_RE.search(body_text)
    if not m_range:
        return None

    start_dt = _parse_cn_datetime(m_range.group("start"), now)
    if start_dt is None:
        return None
    end_dt = _parse_cn_datetime(m_range.group("end"), now, default_date=start_dt)
    if end_dt is None:
        return None
    if end_dt < start_dt:
        # 若跨年（如 12-28 至 01-05）导致 end_dt 年份偏小，自动 +1 年
        try:
            end_dt = end_dt.replace(year=end_dt.year + 1)
        except ValueError:
            pass

    # 提取元数据（如 "题量: 1", "满分: 100"）放入 description
    noise_items = [m.group(0).strip() for m in _NOISE_KV_RE.finditer(body_text)]

    # 从文本中移除时间区间及元数据，提取干净的事项标题
    without_range = (body_text[: m_range.start()] + " " + body_text[m_range.end() :]).strip()
    without_noise = _NOISE_KV_RE.sub(" ", without_range)
    lines = [
        re.sub(r"\s+", " ", ln).strip(" ，,。！？!?；;：:-—")
        for ln in without_noise.splitlines()
    ]
    lines = [ln for ln in lines if ln]
    if not lines:
        return None

    title = re.sub(r"^(?:提醒我|请提醒我)\s*", "", lines[0]).strip(" ，,。：:")
    if not title:
        return None

    extra_desc_parts = lines[1:] + noise_items
    label_str = (m_range.group("label") or "").strip(" ：:")
    range_summary = f"{label_str + ': ' if label_str else ''}{m_range.group('start').strip()} 至 {m_range.group('end').strip()}"
    if range_summary not in extra_desc_parts:
        extra_desc_parts.append(range_summary)
    description = " | ".join(extra_desc_parts) if extra_desc_parts else None

    duration = end_dt - start_dt
    is_ddl_window = (
        bool(_DDL_HINT_RE.search(body_text))
        or duration >= timedelta(hours=12)
        or (start_dt <= now < end_dt)
    )

    if is_ddl_window:
        return NLUResult(
            intent=IntentType.CREATE_MEMO_DDL,
            title=title,
            description=description,
            deadline=end_dt,
            remind_advance_minutes=adv_mins,
        )
    return NLUResult(
        intent=IntentType.CREATE_SCHEDULE,
        title=title,
        description=description,
        start_time=start_dt,
        end_time=end_dt,
        remind_advance_minutes=adv_mins,
    )


def _extract_rrule_and_clean_title(title: str) -> tuple[str, str | None]:
    """从标题末尾提取常见重复规则（如'，并且重复提醒'、'每天重复'、'每周重复'）。"""
    rrule = None
    cleaned = title.strip(" ，。！？!?")
    patterns = [
        (r"[，,\s]*(?:并且|并)?(?:每天|每日)(?:都)?(?:重复)?(?:提醒)?$", "FREQ=DAILY"),
        (r"[，,\s]*(?:并且|并)?每周(?:都)?(?:重复)?(?:提醒)?$", "FREQ=WEEKLY"),
        (r"[，,\s]*(?:并且|并)?每月(?:都)?(?:重复)?(?:提醒)?$", "FREQ=MONTHLY"),
        (r"[，,\s]*(?:并且|并)?(?:每个)?工作日(?:都)?(?:重复)?(?:提醒)?$", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
        (r"[，,\s]*(?:并且|并)?重复提醒(?:我)?$", "FREQ=DAILY"),
    ]
    for pat, rule in patterns:
        if re.search(pat, cleaned):
            rrule = rule
            cleaned = re.sub(pat, "", cleaned).strip(" ，。！？!?")
            break
    return cleaned, rrule


def _extract_advance_minutes(text: str) -> tuple[str, list[int] | None]:
    """提取末尾如 '，提前1天提醒我' / '，提前半天提醒'。"""
    cleaned = text.strip(" ，。！？!?")
    m = re.search(
        rf"[，,\s]*提前\s*(?P<num>{_NUMBER}|半)\s*(?:个)?(?P<unit>分钟|分|小时|钟头|天)(?:提醒(?:我)?)?$",
        cleaned,
    )
    if not m:
        return cleaned, None
    try:
        mins = _duration_to_minutes(m.group("num"), m.group("unit"))
        cleaned = cleaned[: m.start()].strip(" ，。！？!?")
        return cleaned, [mins]
    except ValueError:
        return cleaned, None


class NLUEngine:
    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(config_manager.get("timezone", "Asia/Shanghai")))

    async def parse(self, text: str, has_reply: bool = False) -> NLUResult:
        text = text.strip()
        if not text:
            return NLUResult(intent=IntentType.UNKNOWN)
        now = self._now()
        compact = re.sub(r"[\s，。！!？?]", "", text)
        if has_reply:
            if re.fullmatch(r"(?:已完成|完成了?|搞定了?|做好了?|已做|做完了?|1)", compact):
                return NLUResult(intent=IntentType.COMPLETE_TASK)
            if re.fullmatch(r"(?:取消了?|删除|算了|不做了|不用了|2)", compact):
                return NLUResult(intent=IntentType.CANCEL_TASK)
            match = re.fullmatch(rf"(?:推迟|延后|晚点|推后|晚|过)(?:了)?\s*{_DURATION}(?:吧|后)?(?:再提醒(?:我)?)?", compact)
            if match:
                try:
                    return NLUResult(intent=IntentType.MODIFY_TASK, snooze_minutes=_minutes(match))
                except ValueError:
                    return NLUResult(intent=IntentType.UNKNOWN)
            reply_dt = _parse_quote_reply_time(text, now)
            if reply_dt is not None:
                return NLUResult(intent=IntentType.MODIFY_TASK, start_time=reply_dt, deadline=reply_dt)
        elif re.fullmatch(r"(?:我?还有什么(?:事|事情|任务|作业)?没做(?:完)?(?:的)?|未完成(?:事项|列表)?|待办列表|查(?:看)?(?:日程|待办)|(?:最近)?有什么DDL|查看待办)", compact, re.I):
            return NLUResult(intent=IntentType.QUERY_PENDING)

        if not has_reply:
            # L1-0: 免引用跨端操作（按 #ID 或 事项名称 推迟/改期/完成/取消，0 Token）
            m_id_done = re.fullmatch(r"(?:把)?\s*#?(?P<tid>\d+)\s*(?:号任务)?\s*(?:已完成|完成了?|搞定了?|做好了?|已做|做完了?)", compact) or re.fullmatch(
                r"(?:已完成|完成了?|搞定了?)\s*#?(?P<tid>\d+)", compact
            )
            if m_id_done:
                return NLUResult(intent=IntentType.COMPLETE_TASK, title=f"#{m_id_done.group('tid')}")

            m_id_cancel = re.fullmatch(r"(?:把)?\s*#?(?P<tid>\d+)\s*(?:号任务)?\s*(?:取消了?|删除|算了|不做了)", compact) or re.fullmatch(
                r"(?:取消|删除)\s*#?(?P<tid>\d+)", compact
            )
            if m_id_cancel:
                return NLUResult(intent=IntentType.CANCEL_TASK, title=f"#{m_id_cancel.group('tid')}")

            m_named_snooze = re.fullmatch(
                rf"(?:把|将)?\s*(?P<target>#\d+|.+?)\s*(?:推迟|延后|推后)\s*{_DURATION}(?:吧|后)?(?:再提醒(?:我)?)?",
                text.strip(" ，。！？!?"),
            )
            if m_named_snooze:
                try:
                    t_name = m_named_snooze.group("target").strip()
                    if t_name:
                        return NLUResult(intent=IntentType.MODIFY_TASK, title=t_name, snooze_minutes=_minutes(m_named_snooze))
                except ValueError:
                    pass

            m_named_resched = re.match(
                r"^(?:把|将)?\s*(?P<target>#\d+|.+?)\s*(?:的)?(?:截止时间|开始时间|提醒时间|时间)?(?:是|为|设为|改到|改为|改成|调整到|调整为|推到|推迟到|延后到|换到)\s*(?P<time_expr>.+?)(?:吧|再提醒(?:我)?)?$",
                text.strip(" ，。！？!?"),
            )
            if m_named_resched:
                dt = _parse_cn_datetime(m_named_resched.group("time_expr"), now)
                t_name = m_named_resched.group("target").strip()
                if dt is not None and t_name:
                    return NLUResult(intent=IntentType.MODIFY_TASK, title=t_name, start_time=dt, deadline=dt)

            m_named_done = re.fullmatch(r"(?:把|将)?\s*(?P<target>.+?)\s*(?:设为|标记为)?(?:已完成|做完了|搞定了|做好了)", text.strip(" ，。！？!?"))
            if m_named_done and len(m_named_done.group("target").strip()) >= 2:
                return NLUResult(intent=IntentType.COMPLETE_TASK, title=m_named_done.group("target").strip())

            m_named_cancel = re.fullmatch(r"(?:取消|删除)(?:任务|日程|备忘)?\s*(?P<target>#\d+|.+)", text.strip(" ，。！？!?"))
            if m_named_cancel and len(m_named_cancel.group("target").strip()) >= 2:
                return NLUResult(intent=IntentType.CANCEL_TASK, title=m_named_cancel.group("target").strip())

            # L1-Range: 通用时间区间与多行通知卡片解析（如学习通/雨课堂复制的 "作答时间:10-27 11:52至11-07 11:52"）
            range_res = _parse_notice_or_time_range(text, now)
            if range_res is not None:
                return range_res

            # L1-A: DDL 备忘录句式（支持“事项+时间+截止”、“事项 截止时间: XXX”、“时间+事项+截止”）
            body_text, adv_mins = _extract_advance_minutes(text)
            m_ddl_labeled = re.match(
                r"^(?P<title>.+?)\s*(?:[，,；;\s]+|\n+)(?:截止(?:时间|日期)?|DDL|最晚提交时间|提交截止)\s*(?:是|为|[:：])?\s*(?P<time_expr>.+?)\s*(?:前|之前)?(?:截止)?$",
                body_text,
                flags=re.I | re.S,
            )
            if m_ddl_labeled:
                ddl_dt = _parse_cn_datetime(m_ddl_labeled.group("time_expr"), now)
                title_clean = re.sub(r"^(?:提醒我|请提醒我)\s*", "", m_ddl_labeled.group("title")).strip(" ，。：:\n")
                if ddl_dt is not None and title_clean:
                    return NLUResult(
                        intent=IntentType.CREATE_MEMO_DDL,
                        title=title_clean,
                        deadline=ddl_dt,
                        remind_advance_minutes=adv_mins,
                    )

            m_ddl_tail = re.match(
                rf"^(?P<title>.+?)\s*(?:最晚|最迟)?(?:要在|在|于)?\s*(?P<time_expr>(?:\d{{4}}[年/.-])?\d{{1,2}}(?:月|[-/.])\d{{1,2}}[日号]?.*|{_DURATION}\s*(?:之)?后.*|(?:今天|今日|明天|明日|后天|大后天).*)\s*(?:前|之前)?截止(?:吧)?$",
                body_text,
            )
            m_ddl_head = re.match(
                rf"^(?:在|于)?\s*(?P<time_expr>{_DURATION}\s*(?:之)?后|(?:\d{{4}}[年/.-])?\d{{1,2}}(?:月|[-/.])\d{{1,2}}[日号]?(?:\s*(?:凌晨|早上|上午|中午|下午|傍晚|晚上|晚间)?\s*\d{{1,2}}[:：点时](?:半|\d{{1,2}}分?)?)?|(?:今天|今日|明天|明日|后天|大后天)(?:\s*(?:凌晨|早上|上午|中午|下午|傍晚|晚上|晚间)?\s*(?:\d{{1,2}}|[零〇一二两三四五六七八九十]+)\s*[:：点时](?:半|\d{{1,2}}\s*分?)?)?)\s*(?:前|之前)?\s*(?:把|将|要)?(?P<title>.+?)\s*(?:最晚|最迟)?截止(?:吧)?$",
                body_text,
            )
            m_ddl = m_ddl_head or m_ddl_tail
            if m_ddl:
                ddl_dt = _parse_cn_datetime(m_ddl.group("time_expr"), now)
                title_clean = re.sub(r"^(?:提醒我|请提醒我)\s*", "", m_ddl.group("title")).strip(" ，。：:")
                if ddl_dt is not None and title_clean:
                    return NLUResult(
                        intent=IntentType.CREATE_MEMO_DDL,
                        title=title_clean,
                        deadline=ddl_dt,
                        remind_advance_minutes=adv_mins,
                    )

            # L1-B: 相对时间日程（双向兼容 "提醒我半个小时后拿快递" 与 "半个小时后提醒我拿快递"）
            body_sched, sched_adv = _extract_advance_minutes(text)
            body_sched, rrule = _extract_rrule_and_clean_title(body_sched)
            m_rel1 = re.fullmatch(rf"(?:请)?提醒我\s*{_DURATION}\s*(?:之)?后\s*(?:去|要|得)?(?P<title>.+)", body_sched)
            m_rel2 = re.fullmatch(rf"{_DURATION}\s*(?:之)?后\s*(?:(?:请)?提醒我\s*)?(?:去|要|得)?(?P<title>.+)", body_sched)
            match = m_rel1 or m_rel2
            if match and not re.search(r"截止|最晚|最迟|DDL", match.group("title"), re.I):
                try:
                    minutes = _minutes(match)
                    import dateparser
                    parsed = dateparser.parse(f"in {minutes} minutes", languages=["en"], settings={
                        "RELATIVE_BASE": now, "RETURN_AS_TIMEZONE_AWARE": True,
                        "TIMEZONE": str(now.tzinfo),
                    })
                    return NLUResult(
                        intent=IntentType.CREATE_SCHEDULE,
                        title=match.group("title").strip(" ，。"),
                        start_time=parsed or now + timedelta(minutes=minutes),
                        rrule_str=rrule,
                        remind_advance_minutes=sched_adv,
                    )
                except (ValueError, OverflowError):
                    return NLUResult(intent=IntentType.UNKNOWN)

        return await self._parse_llm(text, has_reply)

    async def parse_message(self, text: str, has_reply: bool = False) -> NLUResult:
        return await self.parse(text, has_reply=has_reply)

    def _normalize_llm_result(self, result: NLUResult, text: str, has_reply: bool, now: datetime) -> NLUResult:
        """对 LLM 返回结果执行通用后处理兜底，防止时间区间误取开始时间或引用回复误判为新建。"""
        if has_reply:
            # 引用已有任务时，只要提取到了任何时间或提醒变更，统一归并为 modify_task
            target_dt = result.deadline or result.end_time or result.start_time
            if result.intent in (IntentType.CREATE_SCHEDULE, IntentType.CREATE_MEMO_DDL, IntentType.UNKNOWN):
                if target_dt or result.snooze_minutes or result.remind_advance_minutes or result.rrule_str:
                    result.intent = IntentType.MODIFY_TASK
                    if target_dt:
                        result.start_time = target_dt
                        result.deadline = target_dt
                    if not re.search(r"标题|改名|叫做|改为", text):
                        result.title = None
            return result

        # 非引用场景：若 LLM 同时给出了 start_time 和 end_time（或原文本是时间段），且属于长跨度/作业/DDL
        if result.start_time and result.end_time and result.end_time > result.start_time:
            span = result.end_time - result.start_time
            if _DDL_HINT_RE.search(text) or span >= timedelta(hours=12) or (result.start_time <= now < result.end_time):
                result.intent = IntentType.CREATE_MEMO_DDL
                result.deadline = result.end_time
                result.start_time = None
                result.end_time = None
        elif result.intent == IntentType.CREATE_MEMO_DDL and not result.deadline:
            result.deadline = result.end_time or result.start_time
        return result

    async def _parse_llm(self, text: str, has_reply: bool) -> NLUResult:
        key = config_manager.get("llm_api_key", "")
        if not key:
            return NLUResult(intent=IntentType.UNKNOWN)
        now = self._now()
        prompt = (
            "你是中文日程与备忘录秘书的意图与槽位提取器。"
            f"当前精确本地时间为 {now.isoformat(timespec='seconds')}，"
            f"星期{['一','二','三','四','五','六','日'][now.weekday()]}，时区 {now.tzinfo}。"
            f"当前消息是否引用了已有任务（has_reply）：{has_reply}。\n"
            "请只输出一个严格的 JSON 对象（不要使用 ```json 代码块包裹），包含以下字段：\n"
            "intent, title, description, start_time, end_time, deadline, offset_minutes, rrule_str, remind_advance_minutes, snooze_minutes（缺失项填 null）。\n"
            "核心规则：\n"
            "1. intent 取值只能是：create_schedule, create_memo_ddl, modify_task, complete_task, cancel_task, query_pending, unknown。\n"
            "2. 当 has_reply=True（用户引用了某条已有事项）时：用户的自然语言（如'截止时间是11-07 11:52'、'改到明天下午3点'、'提前2小时提醒'）是在修改该事项！此时 intent 必须为 modify_task，将新时间同时填入 start_time 和 deadline，且不要把'截止时间'当成 title（除非用户明确要求改标题，否则 title 填 null）。\n"
            "3. 当文本包含时间区间（如 '作答时间:10-27 11:52至11-07 11:52'、'开放时间 A ~ B'）或属于作业/练习/测验/实验/提交任务时：intent 必须为 create_memo_ddl，且必须取区间的【结束时间】（如 11-07 11:52）填入 deadline，绝不能把开始时间当作日程提醒时间！同时把题量、满分等附加信息放入 description，title 只保留干净的事项名（如'课堂练习第五章'）。\n"
            "4. 只有短时的具体活动或会议（如'明天14:00至15:30开组会'）才设为 create_schedule，填入 start_time 与 end_time。\n"
            "5. 若用户表达相对当前时间的偏移（如'半个小时后'、'45分钟后'），把正整数分钟填入 offset_minutes，同时计算 ISO8601 时间填入 start_time 或 deadline。\n"
            "6. 具体日期时刻请转为带时区偏移的 ISO8601 格式（如 2026-11-07T11:52:00+08:00）；若未写年份则默认为当前年份。\n"
            "7. 每天/每周等周期转为 RFC5545 RRULE 字符串（不含 RRULE: 前缀，如 FREQ=DAILY）；remind_advance_minutes 为非负整数列表（如 [1440, 720]）或 null。"
        )
        try:
            base_url = str(config_manager.get("llm_base_url") or "https://open.bigmodel.cn/api/paas/v4/").strip().rstrip("/")
            endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
            timeout_sec = float(config_manager.get("llm_timeout_seconds", 30) or 30)
            req_payload = {
                "model": str(config_manager.get("llm_model", "glm-4-flash")).strip(),
                "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
                "temperature": 0.1,
            }
            status_code, resp_text = await asyncio.to_thread(
                _sync_post_chat_completions, endpoint, str(key), req_payload, timeout_sec
            )
            if status_code != 200:
                raise RuntimeError(f"HTTP {status_code}: {resp_text[:200]}")
            resp_data = json.loads(resp_text)
            choices = resp_data.get("choices") or []
            content = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S).strip()
            match_json = re.search(r"\{.*\}", content, flags=re.S)
            if match_json:
                content = match_json.group(0)
            payload = json.loads(content or "{}")
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")

            if isinstance(payload.get("intent"), str):
                payload["intent"] = payload["intent"].strip().lower()

            offset_val = payload.pop("offset_minutes", None)
            if isinstance(offset_val, (int, float)) and offset_val > 0:
                computed_dt = now + timedelta(minutes=int(offset_val))
                if payload.get("intent") == "create_memo_ddl" and not payload.get("deadline"):
                    payload["deadline"] = computed_dt
                elif payload.get("intent") == "create_schedule" and not payload.get("start_time"):
                    payload["start_time"] = computed_dt
                elif payload.get("intent") == "modify_task" and not payload.get("snooze_minutes"):
                    payload["snooze_minutes"] = int(offset_val)

            if isinstance(payload.get("remind_advance_minutes"), (int, float)):
                payload["remind_advance_minutes"] = [int(payload["remind_advance_minutes"])]

            for dt_field in ("start_time", "end_time", "deadline"):
                val = payload.get(dt_field)
                if isinstance(val, str) and val.strip():
                    cn_dt = _parse_cn_datetime(val, now)
                    if cn_dt is not None:
                        payload[dt_field] = cn_dt

            result = self._normalize_llm_result(NLUResult(**payload), text, has_reply, now)
            if not has_reply and not result.title and result.intent in {IntentType.MODIFY_TASK, IntentType.COMPLETE_TASK, IntentType.CANCEL_TASK}:
                return NLUResult(intent=IntentType.UNKNOWN)
            return result
        except Exception as exc:
            logger.warning("NLU LLM request or validation failed (%s: %s)", type(exc).__name__, exc)
            return NLUResult(intent=IntentType.UNKNOWN)

    async def test_llm_connection(self, base_url: str, api_key: str, model: str) -> tuple[bool, str, int]:
        started = time.perf_counter()
        try:
            if not base_url or not api_key or not model:
                raise ValueError("请填写完整的 Base URL、API Key 与模型名称")
            clean_base = base_url.strip().rstrip("/")
            endpoint = clean_base if clean_base.endswith("/chat/completions") else f"{clean_base}/chat/completions"
            req_payload = {
                "model": model.strip(),
                "messages": [{"role": "user", "content": "Reply OK."}],
                "max_tokens": 16,
            }
            status_code, resp_text = await asyncio.to_thread(
                _sync_post_chat_completions, endpoint, api_key.strip(), req_payload, 20.0
            )
            if status_code != 200:
                err_detail = resp_text[:160]
                return False, f"HTTP {status_code}: {err_detail}", int((time.perf_counter() - started) * 1000)
            data = json.loads(resp_text)
            if not data.get("choices"):
                raise ValueError("Empty model response")
            return True, "模型连接成功", int((time.perf_counter() - started) * 1000)
        except Exception as exc:
            return False, f"模型连接失败（{type(exc).__name__}: {exc}）", int((time.perf_counter() - started) * 1000)


nlu_engine = NLUEngine()
