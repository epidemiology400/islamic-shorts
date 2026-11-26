#!/usr/bin/env python3
"""
produce.py — خط إنتاج الفيديوهات الإسلامية القصيرة (9:16، 1080×1920)

الأوامر:
  python3 produce.py check  EPISODE_DIR      فحص الملف والمصادر والتراخيص دون إخراج
  python3 produce.py fetch  EPISODE_DIR      جلب المقاطع الناقصة من Pixabay (إن توفر المفتاح والشبكة)
  python3 produce.py tts    EPISODE_DIR      توليد التعليق الصوتي من ElevenLabs وتوقيت الترجمة تلقائياً
  python3 produce.py render EPISODE_DIR      الإخراج النهائي MP4

المفاتيح لا تُقرأ إلا من متغيرات البيئة (أو تُحقن من وكيل الشبكة)، ولا تُكتب في أي ملف أو سجل.
"""
import argparse, csv, hashlib, json, os, re, shutil, subprocess, sys, time, unicodedata
from pathlib import Path

W, H, FPS = 1080, 1920, 30
MIN_DUR, MAX_DUR = 60.0, 70.0          # شرط المدة لكل حلقة
ROOT = Path(__file__).resolve().parent
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# ----------------------------------------------------------------- secrets
SECRET_VARS = ("PIXABAY_API_KEY", "ELEVENLABS_API_KEY", "PEXELS_API_KEY")


def secret(name):
    """المفتاح من البيئة، وإلا من ملف خاص خارج المستودع (لا يُطبع ولا يُحفظ في git):
    ~/.islamic-shorts/keys/<NAME>  أو المسار في <NAME>_FILE."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    for f in (os.environ.get(name + "_FILE", ""), str(Path.home() / ".islamic-shorts" / "keys" / name)):
        if f and Path(f).is_file():
            v = Path(f).read_text(encoding="utf-8").strip()
            if v:
                return v
    return None


def redact(text):
    text = str(text)
    for n in SECRET_VARS:
        v = secret(n)
        if v:
            text = text.replace(v, "***")
    text = re.sub(r"(?i)(key|api_key|token|xi-api-key)=([^&\s\"']+)", r"\1=***", text)
    return text


def log(*a):
    print(redact(" ".join(str(x) for x in a)), flush=True)


def die(msg):
    log("✖ " + msg)
    sys.exit(1)


# ----------------------------------------------------------------- helpers
def run(cmd, quiet=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die("ffmpeg فشل:\n" + redact(r.stderr[-2500:]))
    return r


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                        "-show_streams", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"تعذر قراءة الملف: {path}")
    return json.loads(r.stdout)


def media_info(path):
    j = probe(path)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), None)
    if not v:
        die(f"لا يوجد مسار صورة في {path}")
    dur = float(j["format"].get("duration") or v.get("duration") or 0)
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    w, h = int(v["width"]), int(v["height"])
    if abs(rot) in (90, 270):
        w, h = h, w
    return {"w": w, "h": h, "dur": dur, "image": Path(path).suffix.lower() in IMAGE_EXT}


def tc(t):
    t = max(0.0, t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h):d}:{int(m):02d}:{s:05.2f}"


def srt_tc(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fc_families():
    r = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True)
    fams = set()
    for line in r.stdout.splitlines():
        for f in line.split(","):
            fams.add(f.strip().replace("\\", ""))
    return fams


def font_families_in(folder):
    fams = set()
    if not folder.exists():
        return fams
    for f in folder.glob("*.[ot]tf"):
        r = subprocess.run(["fc-scan", "--format", "%{family}\n", str(f)], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            for x in line.split(","):
                fams.add(x.strip())
    return fams


def pick_font(candidates, available):
    for c in candidates:
        if c in available:
            return c
    return candidates[-1]


# ----------------------------------------------------------------- episode
def load_episode(ep_dir):
    ep_dir = Path(ep_dir).resolve()
    p = ep_dir / "episode.json"
    if not p.exists():
        die(f"لا يوجد episode.json في {ep_dir}")
    ep = json.loads(p.read_text(encoding="utf-8"))
    ep["_dir"] = ep_dir
    return ep


def cue_times(ep):
    """قائمة الجمل بتوقيتها النهائي: من episode.json أو من ملف التوقيت الآلي لـ tts."""
    timing_file = ep["_dir"] / "build" / "cue_timing.json"
    cues = ep["cues"]
    if timing_file.exists() and ep.get("audio", {}).get("from_tts"):
        t = json.loads(timing_file.read_text(encoding="utf-8"))
        for c, (a, b) in zip(cues, t):
            c["start"], c["end"] = a, b
    for c in cues:
        if c.get("start") is None or c.get("end") is None:
            die("جملة بلا توقيت: " + c["text"][:40])
    return cues


def resolve_shots(ep, cues, total):
    shots = ep["shots"]
    starts = []
    for s in shots:
        at = s["at"]
        if isinstance(at, (int, float)):
            starts.append(float(at))
        else:
            c = cues[at["cue"] - 1]
            starts.append(c["start"] + at.get("offset", 0.0) if at.get("edge", "start") == "start"
                          else c["end"] + at.get("offset", 0.0))
    starts[0] = 0.0
    out = []
    for i, s in enumerate(shots):
        a = starts[i]
        b = starts[i + 1] if i + 1 < len(shots) else total
        if b - a < 0.6:
            die(f"المشهد {s['id']} قصير جداً ({b - a:.2f} ث)")
        out.append({**s, "start": a, "end": b, "dur": b - a})
    return out


# ----------------------------------------------------------------- licences
LICENSES = {
    # source (lowercase) -> rules
    "pixabay": {"name": "Pixabay Content License", "attribution": "recommended"},
    "pexels": {"name": "Pexels License", "attribution": "recommended"},
    "mixkit": {"name": "Mixkit Video Free License", "attribution": "optional",
               "must_contain": "free"},          # Restricted License = استعمال شخصي فقط → مرفوض
    "coverr": {"name": "Coverr License", "attribution": "optional"},
    "own": {"name": "تصوير/ملكية خاصة", "attribution": "none"},
    "test": {"name": "مقطع اختبار — لا يُنشر", "attribution": "none", "test_only": True},
}


def read_manual_licenses(ep_dir):
    f = ep_dir / "licenses.csv"
    rows = {}
    if f.exists():
        with open(f, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r.get("shot_id"):
                    rows[r["shot_id"].strip()] = {k: (v or "").strip() for k, v in r.items()}
    return rows


def validate_license(rec):
    src = rec.get("source", "").lower()
    rule = LICENSES.get(src)
    if not rule:
        return f"مصدر غير معتمد «{rec.get('source')}». المسموح: {', '.join(LICENSES)}"
    if src not in ("own", "test") and not rec.get("url", "").startswith("http"):
        return "رابط صفحة المقطع مطلوب لإثبات الترخيص"
    lic = rec.get("license", "").lower()
    if rule.get("must_contain") and rule["must_contain"] not in lic:
        return f"ترخيص {rec.get('source')} يجب أن يكون «{rule['name']}» (الترخيص المقيّد للاستعمال الشخصي فقط)"
    return None


def find_manual_clip(ep_dir, shot_id, rec):
    clips = ep_dir / "clips"
    if rec and rec.get("file"):
        p = clips / rec["file"]
        return p if p.exists() else None
    for ext in sorted(VIDEO_EXT | IMAGE_EXT):
        p = clips / f"{shot_id}{ext}"
        if p.exists():
            return p
    return None


def resolve_sources(ep, shots, strict=True):
    """يعيد لكل مشهد: الملف + سجل الترخيص. الأولوية: الرفع اليدوي ثم ذاكرة Pixabay."""
    ep_dir = ep["_dir"]
    manual = read_manual_licenses(ep_dir)
    fetched = {}
    ff = ep_dir / "build" / "fetched.json"
    if ff.exists():
        fetched = json.loads(ff.read_text(encoding="utf-8"))
    problems, resolved = [], []
    for s in shots:
        rec = manual.get(s["id"])
        path = find_manual_clip(ep_dir, s["id"], rec)
        if path:
            if not rec:
                problems.append(f"{s['id']}: الملف {path.name} مرفوع بلا سطر ترخيص في licenses.csv")
                rec = {}
            else:
                err = validate_license(rec)
                if err:
                    problems.append(f"{s['id']}: {err}")
            rec = {**rec, "origin": "manual"}
        elif s["id"] in fetched and Path(fetched[s["id"]]["path"]).exists():
            rec = fetched[s["id"]]
            path = Path(rec["path"])
        else:
            problems.append(f"{s['id']}: لا يوجد مقطع — ارفع clips/{s['id']}.mp4 أو شغّل fetch "
                            f"(بحث مقترح: {s.get('query', '')})")
            resolved.append(None)
            continue
        resolved.append({"path": path, "license": rec})
    if problems and strict:
        log("المشكلات:")
        for p in problems:
            log("  • " + p)
        die(f"{len(problems)} مشكلة تمنع الإخراج")
    return resolved, problems


# ----------------------------------------------------------------- pixabay
def http_get(url, params=None, headers=None, stream=False, timeout=40):
    import requests
    try:
        r = requests.get(url, params=params, headers=headers or {}, timeout=timeout, stream=stream)
    except Exception as e:  # لا نطبع الاستثناء كما هو لأنه قد يحمل الرابط مع المفتاح
        raise RuntimeError(redact(f"{type(e).__name__}: {e}")) from None
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} من {r.url.split('?')[0]}")
    return r


def pixabay_search(query, cache_dir):
    key = secret("PIXABAY_API_KEY")
    if not key:
        raise RuntimeError("PIXABAY_API_KEY غير مضبوط في إعدادات البيئة")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cf = cache_dir / (hashlib.sha1(query.encode()).hexdigest()[:16] + ".json")
    if cf.exists() and time.time() - cf.stat().st_mtime < 24 * 3600:   # شرط Pixabay: تخزين 24 ساعة
        return json.loads(cf.read_text())
    r = http_get("https://pixabay.com/api/videos/", params={
        "key": key, "q": query, "video_type": "film", "safesearch": "true",
        "per_page": 30, "order": "popular"})
    data = r.json()
    cf.write_text(json.dumps(data))
    return data


# وسوم تُستبعد آلياً لمخالفتها ضوابط المحتوى (وجوه/نساء/موسيقى/رموز دينية أخرى/مشاهد غير لائقة)
BLOCK_TAGS = {
    "woman", "women", "girl", "girls", "lady", "female", "bikini", "model", "fashion", "beauty",
    "face", "portrait", "selfie", "couple", "kiss", "love", "romance", "wedding", "party", "dance",
    "dancing", "club", "music", "concert", "guitar", "piano", "singer", "dj", "alcohol", "wine",
    "beer", "cocktail", "bar", "casino", "smoking", "cigarette", "church", "cross", "christmas",
    "jesus", "buddha", "buddhism", "temple", "hindu", "statue", "idol", "halloween", "zodiac",
    "horoscope", "tarot", "skull", "blood", "gun", "weapon", "ai generated", "3d", "animation",
    "cartoon", "illustration", "render", "cgi",
}


def tags_ok(tags):
    t = {x.strip().lower() for x in (tags or "").split(",")}
    words = set()
    for x in t:
        words |= set(x.split())
    return not (t & BLOCK_TAGS or words & BLOCK_TAGS)


def pixabay_pick(hits, need, used):
    best, best_score = None, -1e9
    for h in hits:
        if h["id"] in used or not tags_ok(h.get("tags")):
            continue
        v = h["videos"].get("large") or {}
        if not v.get("url"):
            v = h["videos"].get("medium") or {}
        if not v.get("url"):
            continue
        w, hh, d = v["width"], v["height"], h["duration"]
        score = 0.0
        score += 3 if hh >= w else 0                      # عمودي أصلاً
        score += 2 if max(w, hh) >= 1920 else (1 if max(w, hh) >= 1280 else -3)
        score += 2 if d >= need else (0 if d >= need / 1.5 else -4)
        score += min(h.get("likes", 0), 500) / 250
        if score > best_score:
            best, best_score = (h, v), score
    return best


def pexels_search(query, cache_dir):
    """Pexels: المفتاح في ترويسة Authorization — يصلح لـ network secret (لا يراه الكود)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cf = cache_dir / ("px_" + hashlib.sha1(query.encode()).hexdigest()[:16] + ".json")
    if cf.exists() and time.time() - cf.stat().st_mtime < 24 * 3600:
        return json.loads(cf.read_text())
    key = secret("PEXELS_API_KEY")
    r = http_get("https://api.pexels.com/videos/search",
                 params={"query": query, "orientation": "portrait", "size": "large", "per_page": 30},
                 headers={"Authorization": key} if key else {})
    data = r.json()
    cf.write_text(json.dumps(data))
    return data


def pexels_pick(videos, need, used):
    best, best_score = None, -1e9
    for v in videos:
        pid = f"px{v['id']}"
        slug = v.get("url", "").rstrip("/").split("/")[-1].replace("-", " ")
        if pid in used or not tags_ok(slug.replace(" ", ",")):
            continue
        files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("height")]
        if not files:
            continue
        # أصغر ملف لا يقل عن 1920 في البعد الأطول، وإلا الأكبر المتاح
        files.sort(key=lambda f: max(f["width"], f["height"]))
        f = next((x for x in files if max(x["width"], x["height"]) >= 1920), files[-1])
        w, h, d = f["width"], f["height"], v.get("duration", 0)
        score = (3 if h >= w else 0) + (2 if max(w, h) >= 1920 else -3) + (2 if d >= need else -4)
        if score > best_score:
            best, best_score = (v, f), score
    return best


def provider_order(ep):
    return ep.get("sources", ["pixabay", "pexels"])


def cmd_fetch(ep):
    cues = cue_times(ep)
    total = ep["duration"]
    shots = resolve_shots(ep, cues, total)
    ep_dir = ep["_dir"]
    manual = read_manual_licenses(ep_dir)
    ff = ep_dir / "build" / "fetched.json"
    ff.parent.mkdir(parents=True, exist_ok=True)
    fetched = json.loads(ff.read_text(encoding="utf-8")) if ff.exists() else {}
    rejected = set(ep.get("reject_ids", []))       # مقاطع رُفضت بعد مراجعة contact_sheet
    for k in [k for k, v in fetched.items() if v.get("id") in rejected]:
        del fetched[k]
    used = {v.get("id") for v in fetched.values()} | rejected
    media = ROOT / "cache" / "media"
    media.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    dead = set()                                    # مزودون تعذر الوصول إليهم في هذا التشغيل
    for s in shots:
        if find_manual_clip(ep_dir, s["id"], manual.get(s["id"])) or s["id"] in fetched:
            continue
        queries = [s["query"]] + s.get("alt_queries", [])
        rec = None
        for prov in provider_order(ep):
            if prov in dead:
                continue
            for q in queries:
                try:
                    if prov == "pixabay":
                        data = pixabay_search(q, ROOT / "cache" / "pixabay")
                        got = pixabay_pick(data.get("hits", []), s["dur"], used)
                        if got:
                            h, v = got
                            rec = {"id": h["id"], "dl": v["url"], "source": "Pixabay", "url": h["pageURL"],
                                   "author": h.get("user", ""), "license": "Pixabay Content License",
                                   "info": f"{v['width']}×{v['height']}, {h['duration']} ث"}
                    elif prov == "pexels":
                        data = pexels_search(q, ROOT / "cache" / "pexels")
                        got = pexels_pick(data.get("videos", []), s["dur"], used)
                        if got:
                            v, f = got
                            rec = {"id": f"px{v['id']}", "dl": f["link"], "source": "Pexels", "url": v["url"],
                                   "author": (v.get("user") or {}).get("name", ""), "license": "Pexels License",
                                   "info": f"{f['width']}×{f['height']}, {v.get('duration')} ث"}
                except RuntimeError as e:
                    msg = str(e)
                    short = "محجوب في إعدادات الشبكة" if "Proxy" in msg or "403" in msg else msg[:160]
                    log(f"  · {prov}: {short}")
                    dead.add(prov)
                    break                       # هذا المزود غير متاح، ننتقل للتالي
                if rec:
                    break
            if rec:
                break
        if not rec:
            fail += 1
            log(f"✖ {s['id']}: لم يُعثر على مقطع مناسب — ارفعه يدوياً إلى clips/{s['id']}.mp4")
            continue
        dest = media / f"{rec['source'].lower()}_{rec['id']}.mp4"
        if not dest.exists():
            r = http_get(rec.pop("dl"), stream=True, timeout=180)
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        rec.pop("dl", None)
        used.add(rec["id"])
        info = rec.pop("info")
        fetched[s["id"]] = {"path": str(dest), "origin": "api", "needs_review": True, **rec}
        ok += 1
        log(f"✔ {s['id']}: {rec['source']} #{rec['id']} ({info})")
    ff.write_text(json.dumps(fetched, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"جُلب {ok}، تعذر {fail}. المقاطع الآلية تحتاج مراجعة بصرية قبل النشر (contact_sheet.jpg).")


# ----------------------------------------------------------------- ElevenLabs TTS + alignment
AR_DIAC = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")


def norm_ar(s):
    s = AR_DIAC.sub("", s)
    s = re.sub(r"\[[^\]]*\]", "", s)                          # وسوم الأداء
    s = s.replace("ﷺ", "صلى الله عليه وسلم")
    s = re.sub("[إأآٱ]", "ا", s).replace("ى", "ي").replace("ة", "ه")
    return "".join(ch for ch in s if unicodedata.category(ch)[0] == "L")


def align_cues(cues, chars, starts, ends):
    """يطابق نص كل جملة (أو حقل spoken) مع سلسلة الحروف المنطوقة ويعيد [بداية، نهاية]."""
    stream, idx = [], []
    for i, ch in enumerate(chars):
        n = norm_ar(ch)
        for c in n:
            stream.append(c)
            idx.append(i)
    S = "".join(stream)
    pos, out = 0, []
    for c in cues:
        target = norm_ar(c.get("spoken", c["text"].split("\n")[0] if c.get("ref_second_line") else c["text"]))
        if not target:
            out.append(None)
            continue
        k = S.find(target, pos)
        if k < 0:
            k = S.find(target[: max(6, len(target) // 2)], pos)
            if k < 0:
                raise RuntimeError("تعذرت محاذاة: " + c["text"][:30])
            last = min(len(S) - 1, k + len(target) - 1)
        else:
            last = k + len(target) - 1
        out.append([starts[idx[k]], ends[idx[last]]])
        pos = last + 1
    for i, v in enumerate(out):                     # جملة غير منطوقة (مرجع) تأخذ وقت ما قبلها
        if v is None:
            prev = out[i - 1] if i else [0, 0.5]
            out[i] = [prev[1], prev[1] + 1.2]
    return out


def cmd_tts(ep):
    import base64
    a = ep["audio"]
    voice = a["voice_id"]
    text = a["script"]
    key = secret("ELEVENLABS_API_KEY")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["xi-api-key"] = key
    # بدون مفتاح في البيئة: يعتمد على «network secret» يحقنه وكيل الشبكة في الطلب.
    import requests
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps"
    try:
        r = requests.post(url, params={"output_format": "mp3_44100_192"}, headers=headers, timeout=300,
                          json={"text": text, "model_id": a.get("model_id", "eleven_v3"),
                                "voice_settings": a.get("voice_settings",
                                                        {"stability": 0.5, "similarity_boost": 0.8})})
    except Exception as e:
        die(redact(f"تعذر الاتصال بـ ElevenLabs: {type(e).__name__}"))
    if r.status_code == 401:
        die("ElevenLabs رفض الطلب (401): أضف المفتاح كـ network secret للمضيف api.elevenlabs.io "
            "بالترويسة xi-api-key، أو كمتغير بيئة ELEVENLABS_API_KEY")
    if r.status_code >= 400:
        die(f"ElevenLabs: HTTP {r.status_code}")
    j = r.json()
    b = ep["_dir"] / "build"
    b.mkdir(exist_ok=True)
    (b / "vo_raw.mp3").write_bytes(base64.b64decode(j["audio_base64"]))
    al = j.get("normalized_alignment") or j["alignment"]
    times = align_cues(ep["cues"], al["characters"], al["character_start_times_seconds"],
                       al["character_end_times_seconds"])
    (b / "cue_timing.json").write_text(json.dumps(times), encoding="utf-8")
    log(f"✔ التعليق الصوتي: build/vo_raw.mp3 — وُقّتت {len(times)} جملة تلقائياً")


def words_to_chars(words):
    chars, st, en = [], [], []
    for w in words:
        t = w.get("text", w.get("word", ""))
        if w.get("type", "word") != "word" or t.startswith("["):
            continue
        a, b = float(w["start"]), float(w["end"])
        n = max(1, len(t))
        for k, ch in enumerate(t):
            chars.append(ch)
            st.append(a + (b - a) * k / n)
            en.append(a + (b - a) * (k + 1) / n)
        chars.append(" "); st.append(b); en.append(b)
    return chars, st, en


def transcribe_words(audio_path, lang="ar"):
    """1) Whisper محلي إن كان مثبتاً  2) ElevenLabs Scribe عبر الـ API (المفتاح من البيئة أو network secret)."""
    try:
        from faster_whisper import WhisperModel          # noqa
        m = WhisperModel(os.environ.get("WHISPER_MODEL", "medium"), compute_type="int8")
        segs, _ = m.transcribe(str(audio_path), language=lang, word_timestamps=True)
        return [{"text": w.word.strip(), "start": w.start, "end": w.end} for s in segs for w in s.words], "faster-whisper"
    except ImportError:
        pass
    try:
        import whisper                                    # noqa
        m = whisper.load_model(os.environ.get("WHISPER_MODEL", "medium"))
        r = m.transcribe(str(audio_path), language=lang, word_timestamps=True)
        return [{"text": w["word"].strip(), "start": w["start"], "end": w["end"]}
                for s in r["segments"] for w in s.get("words", [])], "whisper"
    except ImportError:
        pass
    import requests
    key = secret("ELEVENLABS_API_KEY")
    try:
        with open(audio_path, "rb") as fh:
            r = requests.post("https://api.elevenlabs.io/v1/speech-to-text",
                              headers={"xi-api-key": key} if key else {},
                              data={"model_id": "scribe_v1", "language_code": "ara",
                                    "timestamps_granularity": "word", "tag_audio_events": "false"},
                              files={"file": (Path(audio_path).name, fh)}, timeout=300)
    except Exception as e:
        die(redact(f"تعذر الاتصال بخدمة التفريغ: {type(e).__name__}"))
    if r.status_code >= 400:
        die(f"ElevenLabs Scribe: HTTP {r.status_code}")
    return r.json().get("words", []), "elevenlabs-scribe"


def cmd_align(ep, words_file=None):
    """مزامنة الترجمة مع ملف صوتي موجود: تفريغ بتوقيت كل كلمة ثم مطابقة نص كل جملة."""
    audio = ep["_dir"] / ep["audio"]["file"]
    if words_file:
        words, engine = json.loads(Path(words_file).read_text(encoding="utf-8")), "ملف كلمات"
        words = words.get("words", words) if isinstance(words, dict) else words
    else:
        words, engine = transcribe_words(audio)
    chars, st, en = words_to_chars(words)
    times = align_cues(ep["cues"], chars, st, en)
    # تمديد كل سطر حتى ما قبل التالي بقليل (أقصى 0.8 ث) ليقرأه المشاهد
    for i in range(len(times) - 1):
        times[i][1] = min(times[i + 1][0] - 0.04, times[i][1] + 0.8)
    times[-1][1] += 1.2
    for c, (a, b) in zip(ep["cues"], times):
        c["start"], c["end"] = round(a, 3), round(b, 3)
    p = ep["_dir"] / "episode.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["cues"] = [{k: v for k, v in c.items()} for c in ep["cues"]]
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"✔ وُقّتت {len(times)} جملة ({engine}) وحُفظت في episode.json")


# ----------------------------------------------------------------- patch: تصحيح عبارة في التسجيل
def adaptive_db(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    m = re.findall(r"mean_volume: (-?[\d.]+) dB", r)
    mean = float(m[-1]) if m else -20.0
    return max(-42.0, min(-20.0, mean - 13.0))      # الصمت = أدنى من متوسط الكلام بـ 13 dB


def silences(path, db=None, d=0.18):
    if db is None:
        db = adaptive_db(path)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", f"silencedetect=noise={db}dB:d={d}",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    st = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r)]
    en = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r)]
    return list(zip(st, en))


def speech_bounds(path):
    """بداية ونهاية الكلام الفعلي في ملف (بقص الصمت من الطرفين)."""
    dur = float(probe(path)["format"]["duration"])
    sil = silences(path, d=0.05)
    a, b = 0.0, dur
    if sil and sil[0][0] <= 0.02:
        a = sil[0][1]
    if sil and sil[-1][1] >= dur - 0.05:
        b = sil[-1][0]
    return max(0, a - 0.03), min(dur, b + 0.05), dur


def cmd_patch(ep, start, end, new_file):
    """يستبدل الكلام بين start وend (ثوانٍ تقريبية في ملف التعليق) بملف جديد،
    ويلتقط حدود الصمت الطبيعية، ثم يزيح توقيت كل ما بعده."""
    ep_dir = ep["_dir"]
    vo = ep_dir / ep["audio"]["file"]
    sil = silences(vo)
    # الحد الأيسر: نهاية أقرب صمت قبل start؛ الحد الأيمن: بداية أقرب صمت بعد end
    total_d = float(probe(vo)["format"]["duration"])
    dist = lambda t, iv: 0.0 if iv[0] <= t <= iv[1] else min(abs(t - iv[0]), abs(t - iv[1]))
    lc = min(sil, key=lambda iv: dist(start, iv)) if sil else None
    rc = min(sil, key=lambda iv: dist(end, iv)) if sil else None
    left = lc[1] if lc and dist(start, lc) < 0.8 else max(0.0, start)
    right = rc[0] if rc and dist(end, rc) < 0.8 else min(total_d, end)
    if right - left < 0.3:
        die(f"حدود غير صالحة ({left:.2f}→{right:.2f}) — راجع --at")
    a, b, _ = speech_bounds(new_file)
    new_len = b - a
    delta = new_len - (right - left)
    log(f"استبدال {left:.2f}→{right:.2f} ({right - left:.2f} ث) بمقطع {new_len:.2f} ث — إزاحة {delta:+.2f} ث")
    backup = vo.with_name(vo.stem + f".before_patch_{int(time.time())}" + vo.suffix)
    shutil.copy2(vo, backup)
    tmp = vo.with_name("_patched.wav")
    fc = (f"[0:a]atrim=0:{left:.3f},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=mono[p1];"
          f"[1:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_rates=48000:channel_layouts=mono,"
          f"loudnorm=I=-16:TP=-2,aresample=48000,afade=t=in:d=0.02,afade=t=out:st={max(0, new_len - 0.04):.3f}:d=0.04[p2];"
          f"[0:a]atrim={right:.3f},asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=mono[p3];"
          f"[p1][p2][p3]concat=n=3:v=0:a=1,loudnorm=I=-14:TP=-1.5:LRA=7[o]")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(vo), "-i", str(new_file),
         "-filter_complex", fc, "-map", "[o]", "-ar", "48000", str(tmp)])
    tmp.replace(vo)
    p = ep_dir / "episode.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    for c in data["cues"]:
        for k in ("start", "end"):
            if c[k] >= right - 0.05:
                c[k] = round(c[k] + delta, 3)
            elif c[k] > left and k == "end":
                c[k] = round(c[k] + delta, 3)       # الجملة المستبدلة نفسها تمتد/تنكمش
    for s in data["shots"]:
        if isinstance(s["at"], (int, float)) and s["at"] >= right - 0.05:
            s["at"] = round(s["at"] + delta, 3)
    data["duration"] = round(data["duration"] + delta, 3)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"✔ صُحِّح التسجيل (نسخة احتياطية: {backup.name}) — المدة الجديدة {data['duration']:.2f} ث")


# ----------------------------------------------------------------- doctor: الأدوات والاتصال
def cmd_doctor():
    import importlib.util, requests
    log("① الأدوات")
    v = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True).stdout
    conf = subprocess.run(["ffmpeg", "-hide_banner", "-buildconf"], capture_output=True, text=True).stdout
    log("  ✔ " + v.splitlines()[0][:60])
    for lib in ("libx264", "libass", "libharfbuzz", "libfribidi", "libfreetype"):
        log(f"  {'✔' if 'enable-' + lib in conf else '✖'} {lib}")
    for mod in ("requests", "numpy", "faster_whisper", "whisper"):
        ok = importlib.util.find_spec(mod) is not None
        note = "" if ok or mod not in ("faster_whisper", "whisper") else " (اختياري — يُستبدل بـ ElevenLabs Scribe)"
        log(f"  {'✔' if ok else '✖'} {mod}{note}")
    fams = fc_families() | font_families_in(ROOT / "fonts")
    for f in ("Amiri", "Amiri Quran", "Cairo", "Tajawal", "DejaVu Sans", "FreeSerif"):
        log(f"  {'✔' if f in fams else '·'} خط {f}")

    log("② المفاتيح (يُعرض وجودها فقط، لا قيمتها)")
    for n in SECRET_VARS:
        log(f"  {'✔ مضبوط في البيئة' if secret(n) else '· غير موجود في البيئة (قد يُحقن كـ network secret)'}: {n}")

    log("③ اختبار الاتصال")

    def probe_req(name, url, **kw):
        try:
            r = requests.get(url, timeout=15, **kw)
            pe = r.headers.get("X-Proxy-Error", "")
            if pe:
                return log(f"  ✖ {name}: محجوب من قائمة النطاقات المسموحة ({pe})")
            msg = {200: "✔ متصل ويعمل", 401: "✖ وصل للخدمة لكن المفتاح مفقود/غير صحيح",
                   403: "✖ مرفوض (403)", 429: "· متصل لكن تجاوزت حد الطلبات"}.get(r.status_code, f"· HTTP {r.status_code}")
            log(f"  {msg}: {name}")
        except Exception as e:
            log(f"  ✖ {name}: لا اتصال ({type(e).__name__}) — النطاق غير مسموح في إعدادات الشبكة")

    pk = secret("PIXABAY_API_KEY")
    if pk:
        probe_req("Pixabay API", "https://pixabay.com/api/videos/", params={"key": pk, "q": "sky", "per_page": 3})
    else:
        probe_req("Pixabay (بلا مفتاح)", "https://pixabay.com/api/videos/", params={"q": "sky"})
    probe_req("Pixabay CDN (التنزيل)", "https://cdn.pixabay.com/")
    xk = secret("PEXELS_API_KEY")
    probe_req("Pexels API", "https://api.pexels.com/videos/search",
              params={"query": "sky", "per_page": 1}, headers={"Authorization": xk} if xk else {})
    probe_req("Pexels CDN (التنزيل)", "https://videos.pexels.com/")
    ek = secret("ELEVENLABS_API_KEY")
    probe_req("ElevenLabs API", "https://api.elevenlabs.io/v1/user/subscription",
              headers={"xi-api-key": ek} if ek else {})


# ----------------------------------------------------------------- grading
GRADES = {
    # ليل سينمائي: ظلال زرقاء مخضرة، تشبع منخفض
    "night": "eq=contrast=1.07:brightness=-0.035:saturation=0.78:gamma=0.96,"
             "colorbalance=rs=-0.05:gs=0.0:bs=0.09:rm=-0.03:gm=0.0:bm=0.05:rh=-0.02:bh=0.02,"
             "curves=all='0/0.02 0.25/0.21 0.5/0.49 0.78/0.80 1/0.97'",
    # الساعة الزرقاء قبل الفجر: انتقال
    "twilight": "eq=contrast=1.05:brightness=-0.01:saturation=0.9:gamma=0.98,"
                "colorbalance=rs=-0.01:bs=0.05:rm=0.03:bm=0.01:rh=0.04:bh=-0.02,"
                "curves=all='0/0.02 0.25/0.22 0.5/0.5 0.8/0.82 1/0.98'",
    # الفجر الذهبي: إضاءات دافئة
    "dawn": "eq=contrast=1.06:saturation=1.06:gamma=1.0,"
            "colorbalance=rs=0.03:bs=-0.04:rm=0.07:gm=0.025:bm=-0.05:rh=0.06:gh=0.02:bh=-0.05,"
            "curves=all='0/0.015 0.25/0.22 0.5/0.51 0.78/0.81 1/0.98'",
    "neutral": "eq=contrast=1.04:saturation=0.95",
}


def shot_filter(info, dur, look, motion, src_in):
    """تكبير/قص إلى 9:16، حركة كاميرا ناعمة، تدرج لوني، حبيبات خفيفة، تظليل أطراف."""
    Z = 1.0 + motion.get("zoom", 0.06)
    d = max(dur, 0.1)
    if motion.get("dir", "in") == "in":
        zexpr = f"(1+({Z}-1)*t/{d:.3f})"
    else:
        zexpr = f"({Z}-({Z}-1)*t/{d:.3f})"
    f = [
        f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos",
        f"crop={W}:{H}",
        f"scale=w='trunc({W}*{zexpr}/2)*2':h=-2:eval=frame:flags=bicubic",
        f"crop={W}:{H}",
        GRADES.get(look, GRADES["neutral"]),
        "vignette=angle=PI/5.5",
        "noise=alls=2:allf=t",
        f"fps={FPS}", "setsar=1", "format=yuv420p",
    ]
    return ",".join(f)


# ----------------------------------------------------------------- subtitles (ASS)
def ass_color(hexrgb, alpha=0):
    hexrgb = hexrgb.lstrip("#")
    r, g, b = hexrgb[0:2], hexrgb[2:4], hexrgb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def load_words(ep):
    f = ep["_dir"] / "build" / "words.json"
    if not f.exists():
        return None
    w = json.loads(f.read_text(encoding="utf-8"))
    w = w.get("words", w) if isinstance(w, dict) else w
    return [x for x in w if x.get("type", "word") == "word" and not str(x.get("text", "")).startswith("[")]


def karaoke_text(c, words, next_start, gold):
    """ترجمة تتبع الكلام: كل كلمة معروضة تُضاء لحظة نطقها (k)."""
    ws = [w for w in words if c["start"] - 0.06 <= float(w["start"]) < next_start - 0.02]
    if not ws:
        return None
    lines = c["text"].split("\n")
    toks = [(li, t) for li, ln in enumerate(lines) for t in ln.split()]
    if not toks:
        return None
    if len(ws) == len(toks):
        starts = [float(w["start"]) for w in ws]
    else:
        a, b = float(ws[0]["start"]), float(ws[-1]["end"])
        L = [len(t) + 1 for _, t in toks]
        tot, acc, starts = sum(L), 0, []
        for n in L:
            starts.append(a + (b - a) * acc / tot)
            acc += n
    hl = sorted(c.get("highlight", []), key=len, reverse=True)
    hl_words = {x for h in hl for x in h.split()}
    out, t0 = [], c["start"]
    lead = max(0, round((starts[0] - t0) * 100))
    if lead:
        out.append(r"{\k%d}" % lead)
    for i, (li, t) in enumerate(toks):
        end = starts[i + 1] if i + 1 < len(toks) else starts[i] + 0.45
        k = max(1, round((end - starts[i]) * 100))
        sep = ""
        if i + 1 < len(toks):
            sep = r"\N" if toks[i + 1][0] != li else " "
        tt = t.replace("{", "(").replace("}", ")")
        col = r"\c" + gold if t in hl_words else ""
        out.append(r"{\k%d%s}" % (k, col) + tt + (r"{\c&HFFFFFF&}" if col else "") + sep)
    return "".join(out)


def build_ass(ep, cues, out_path, fonts):
    st = ep.get("style", {})
    gold = ass_color(st.get("accent", "#D4A64A"))
    white = ass_color("#FFFFFF")
    soft = ass_color("#000000", 0x70)
    mv = st.get("margin_bottom", 560)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Narr,{fonts['narration']},{st.get('size', 68)},{white},{white},{soft},{soft},-1,0,0,0,100,100,0,0,1,3.2,2.2,2,110,150,{mv},-1
Style: Hadith,{fonts['hadith']},{st.get('size_hadith', 70)},{ass_color('#F6EEDC')},{white},{soft},{soft},-1,0,0,0,100,100,0,0,1,3.2,2.2,2,100,140,{mv},-1
Style: Hook,{fonts['narration']},{st.get('size_hook', 92)},{gold},{white},{soft},{ass_color('#000000', 0x60)},-1,0,0,0,100,100,0,0,1,4,3,8,90,90,{st.get('hook_margin_top', 330)},-1
Style: Badge,{fonts['narration']},{st.get('size_badge', 40)},{ass_color('#EADCB8')},{white},{soft},{ass_color('#000000', 0x88)},-1,0,0,0,100,100,0,0,3,10,0,9,60,70,{st.get('badge_margin_top', 300)},-1
Style: Quran,{fonts['quran']},{st.get('size_quran', 76)},{gold},{white},{soft},{soft},-1,0,0,0,100,100,0,0,1,3.4,2.4,2,100,140,{mv},-1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    hook = ep.get("hook")
    if hook and hook.get("text"):
        ht = hook["text"].replace("{", "(").replace("}", ")").replace("\n", r"\N")
        for w in sorted(hook.get("highlight", []), key=len, reverse=True):
            ht = ht.replace(w, r"{\c" + white + "}" + w + r"{\c}", 1)
        lines.append(f"Dialogue: 1,{tc(hook.get('start', 0.0))},{tc(hook.get('end', 3.0))},Hook,,0,0,0,,"
                     + r"{\fad(0,280)\fscx112\fscy112\t(0,260,\fscx100\fscy100)}" + ht)
    for bd in ep.get("badges", []):                 # توثيق مختصر ثابت في زاوية الشاشة أثناء النص الشرعي
        a_, b_ = bd["cues"]
        bt = bd["text"].replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 2,{tc(cues[a_ - 1]['start'])},{tc(cues[b_ - 1]['end'])},Badge,,0,0,0,,"
                     + r"{\fad(250,250)}" + bt)
    words = load_words(ep) if st.get("karaoke") else None
    for ci, c in enumerate(cues):
        style = {"quran": "Quran", "hadith": "Hadith"}.get(c.get("style", ""), "Narr")
        if words and style == "Narr" and not c.get("ref_second_line"):
            nxt = cues[ci + 1]["start"] if ci + 1 < len(cues) else c["end"] + 5
            kt = karaoke_text(c, words, nxt, gold)
            if kt:
                lines.append(f"Dialogue: 0,{tc(c['start'])},{tc(c['end'])},Narr,,0,0,0,,"
                             + r"{\fad(120,200)\2c" + ass_color(st.get("karaoke_dim", "#9A9A9A")) + "}" + kt)
                continue
        parts = c["text"].split("\n")
        body = []
        for j, p in enumerate(parts):
            p = p.replace("{", "(").replace("}", ")")
            if j > 0 and c.get("ref_second_line"):
                body.append(r"{\fn" + fonts["narration"] + r"\fs42\c" + ass_color("#D9D9D9") + r"\b0}" + p + r"{\r}")
                continue
            for w in sorted(c.get("highlight", []), key=len, reverse=True):
                if w in p:
                    p = p.replace(w, r"{\c" + gold + "}" + w + r"{\c}", 1) if style != "Quran" else p
            body.append(p)
        txt = r"{\fad(160,200)}" + r"\N".join(body)
        lines.append(f"Dialogue: 0,{tc(c['start'])},{tc(c['end'])},{style},,0,0,0,,{txt}")
    out_path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8-sig")


def write_srt(cues, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(cues, 1):
            f.write(f"{i}\n{srt_tc(c['start'])} --> {srt_tc(c['end'])}\n{c['text']}\n\n")


# ----------------------------------------------------------------- render
def cmd_check(ep):
    cues = cue_times(ep)
    shots = resolve_shots(ep, cues, ep["duration"])
    res, problems = resolve_sources(ep, shots, strict=False)
    log(f"المدة {ep['duration']:.2f} ث — {len(cues)} جملة — {len(shots)} مشهد")
    for s, r in zip(shots, res):
        state = "✔" if r else "✖"
        src = (r["license"].get("source", "?") if r else "—")
        log(f" {state} {s['id']:>4} {s['start']:6.2f}→{s['end']:6.2f} ({s['dur']:.2f}ث) {src:<8} {s.get('desc', '')}")
    if problems:
        log(f"\n{len(problems)} مشكلة:")
        for p in problems:
            log("  • " + p)
    else:
        log("\n✔ جاهز للإخراج")


def split_long_shots(shots, res, max_shot):
    """يقسّم المشهد الأطول من max_shot إلى أجزاء من المقطع نفسه (تسلسل زمني متصل)
    مع تغيّر هادئ في اتجاه الحركة عند كل قطع (بلا قفزات صاخبة): إيقاع أسرع دون مقاطع إضافية."""
    import math
    out_s, out_r = [], []
    for s, r in zip(shots, res):
        k = math.ceil(s["dur"] / max_shot - 0.25)
        info = media_info(r["path"]) if k > 1 else None
        if k <= 1 or info["image"] or info["dur"] < s["dur"] + 0.5:
            out_s.append(s); out_r.append(r)
            continue
        d = s["dur"] / k
        base = float(s["in"]) if "in" in s else max(0.0, (info["dur"] - s["dur"]) * 0.4)
        m = s.get("motion", {})
        for j in range(k):
            p = {**s, "start": s["start"] + j * d, "end": s["start"] + (j + 1) * d, "dur": d,
                 "in": base + j * d, "_part": j}
            if j:
                p["id"] = f"{s['id']}.{j + 1}"
                p["transition"] = "cut"
                p["motion"] = ({"zoom": max(0.06, m.get("zoom", 0.05) + 0.02), "dir": "out"} if j % 2
                               else {"zoom": m.get("zoom", 0.05), "dir": "in"})
            out_s.append(p); out_r.append(r)
    return out_s, out_r


def cmd_render(ep, args):
    ep_dir = ep["_dir"]
    b = ep_dir / "build"
    b.mkdir(exist_ok=True)
    out_dir = ep_dir / "out"
    out_dir.mkdir(exist_ok=True)
    total = ep["duration"]
    cues = cue_times(ep)
    shots = resolve_shots(ep, cues, total)
    if not (MIN_DUR <= total <= MAX_DUR):
        die(f"مدة الحلقة {total:.2f} ث خارج المدى المطلوب ({MIN_DUR:.0f}–{MAX_DUR:.0f} ث): "
            "عدّل النص (احذف أو أضف جملة) وأعد tts، أو عدّل الوقفات")
    res, _ = resolve_sources(ep, shots, strict=True)
    test_only = any(LICENSES.get(r["license"].get("source", "").lower(), {}).get("test_only") for r in res)
    report_shots, report_res = shots, res
    if ep.get("style", {}).get("max_shot"):
        shots, res = split_long_shots(shots, res, float(ep["style"]["max_shot"]))
        log(f"الإيقاع: {len(report_shots)} مشهد ← {len(shots)} لقطة (أقصى {ep['style']['max_shot']} ث)")

    # ---- الخطوط
    user_fonts = ROOT / "fonts"
    avail = fc_families() | font_families_in(user_fonts)
    fcfg = ep.get("fonts", {})
    fonts = {
        "narration": pick_font(fcfg.get("narration", []) + ["Cairo", "Tajawal", "Noto Kufi Arabic", "DejaVu Sans"], avail),
        "hadith": pick_font(fcfg.get("hadith", []) + ["Amiri", "Noto Naskh Arabic", "FreeSerif"], avail),
        "quran": pick_font(fcfg.get("quran", []) + ["Amiri Quran", "Amiri", "KFGQPC Uthmanic Script HAFS", "FreeSerif"], avail),
    }
    log("الخطوط:", fonts)

    # ---- المشاهد
    trans = []
    for i, s in enumerate(shots):
        t = s.get("transition", "cut") if i else "none"
        trans.append({"none": 0.0, "cut": 2 / FPS, "dissolve": 0.5, "dip": 0.6}.get(t, 2 / FPS))
    seg_files = []
    for i, (s, r) in enumerate(zip(shots, res)):
        d_in = trans[i] / 2
        d_out = trans[i + 1] / 2 if i + 1 < len(shots) else 0
        L = s["dur"] + d_in + d_out
        info = media_info(r["path"])
        src_in = float(s.get("in", -1))
        inputs = []
        speed = 1.0
        if info["image"]:
            inputs = ["-loop", "1", "-framerate", str(FPS), "-t", f"{L:.3f}", "-i", str(r["path"])]
        else:
            avail_dur = info["dur"]
            if avail_dur >= L:
                if src_in < 0:
                    src_in = max(0.0, (avail_dur - L) * 0.4)
                src_in = min(src_in, max(0.0, avail_dur - L))
                inputs = ["-ss", f"{src_in:.3f}", "-t", f"{L:.3f}", "-i", str(r["path"])]
            elif avail_dur * 1.6 >= L:
                speed = avail_dur / L * 0.995              # إبطاء خفيف بدل التكرار
                inputs = ["-i", str(r["path"])]
            else:
                inputs = ["-stream_loop", "-1", "-t", f"{L:.3f}", "-i", str(r["path"])]
        vf = shot_filter(info, L, s.get("look", "neutral"), s.get("motion", {}), src_in)
        if speed != 1.0:
            vf = f"setpts=PTS/{speed:.5f}," + vf
        vf += f",trim=duration={L:.3f},setpts=PTS-STARTPTS"
        seg = b / f"seg_{i:02d}.mp4"
        st_ = r["path"].stat()
        key = hashlib.sha1(json.dumps([str(r["path"]), st_.st_mtime, st_.st_size, inputs, vf]).encode()).hexdigest()
        kf = seg.with_suffix(".key")
        if seg.exists() and kf.exists() and kf.read_text() == key:
            seg_files.append((seg, L))
            log(f"  ▸ {s['id']} (من الذاكرة)")
            continue
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-an", "-vf", vf,
             "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
             "-pix_fmt", "yuv420p", str(seg)])
        got = float(probe(seg)["format"]["duration"])
        if got + 0.05 < L:
            die(f"المشهد {s['id']} أقصر من المطلوب ({got:.2f} < {L:.2f})")
        kf.write_text(key)
        seg_files.append((seg, L))
        log(f"  ▸ {s['id']} {s['start']:6.2f}→{s['end']:6.2f}  {r['path'].name}")

    # ---- الربط بانتقالات متمركزة على نقاط القطع (لا تزيح التزامن)
    fc, prev = [], "[0:v]"
    for i in range(1, len(seg_files)):
        kind = shots[i].get("transition", "cut")
        x = {"dip": "fadeblack"}.get(kind, "fade")
        off = shots[i]["start"] - trans[i] / 2
        lab = f"[x{i}]"
        fc.append(f"{prev}[{i}:v]xfade=transition={x}:duration={trans[i]:.4f}:offset={off:.4f}{lab}")
        prev = lab
    ass = b / "subs.ass"
    build_ass(ep, cues, ass, fonts)
    fonts_dir = str(user_fonts if user_fonts.exists() else "/usr/share/fonts")
    fade_out = ep.get("fade_out", 1.0)
    fc.append(f"{prev}trim=duration={total:.3f},"
              f"ass='{ass}':fontsdir='{fonts_dir}',"
              f"fade=t=in:st=0:d=0.25,fade=t=out:st={total - fade_out:.3f}:d={fade_out},format=yuv420p[v]")

    # ---- الصوت: التعليق + أصوات البيئة (اختياري، بتراخيص)
    a = ep["audio"]
    vo = ep_dir / a["file"] if not a.get("from_tts") else b / "vo_raw.mp3"
    if not vo.exists():
        die(f"ملف التعليق الصوتي غير موجود: {vo}")
    ain = ["-i", str(vo)]
    nin = len(seg_files)
    afc = [f"[{nin}:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.0[vo]"]
    mix = ["[vo]"]
    amb_records = []
    for k, amb in enumerate(a.get("ambience", [])):
        p = ep_dir / amb["file"]
        if not p.exists():
            log(f"  (تخطي صوت البيئة الناقص: {amb['file']})")
            continue
        err = validate_license(amb)
        if err:
            die(f"صوت البيئة {amb['file']}: {err}")
        amb_records.append(amb)
        ain += ["-stream_loop", "-1", "-i", str(p)]
        j = nin + 1 + len(amb_records) - 1
        st, en = amb.get("start", 0.0), amb.get("end", total)
        afc.append(f"[{j}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{en - st:.3f},"
                   f"volume={amb.get('db', -24)}dB,afade=t=in:d=0.8,afade=t=out:st={max(0, en - st - 1.2):.3f}:d=1.2,"
                   f"adelay={int(st * 1000)}|{int(st * 1000)}[amb{k}]")
        mix.append(f"[amb{k}]")
    afc.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=first,"
               f"apad=whole_dur={total:.3f},atrim=0:{total:.3f},"
               f"afade=t=out:st={total - 1.2:.3f}:d=1.2,"
               f"loudnorm=I=-14:TP=-1.5:LRA=9,aresample=48000[a]")

    name = ep.get("slug", "episode") + ("_TEST" if test_only else "")
    out = out_dir / f"{name}.mp4"
    preset = "slow" if args.best else "veryfast"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for seg, _ in seg_files:
        cmd += ["-i", str(seg)]
    cmd += ain + ["-filter_complex", ";".join(fc + afc), "-map", "[v]", "-map", "[a]",
                  "-c:v", "libx264", "-preset", preset, "-crf", "19", "-maxrate", "12M", "-bufsize", "24M",
                  "-profile:v", "high",
                  "-level", "4.1", "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
                  "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                  "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
                  "-metadata", f"title={ep.get('title', '')}", "-t", f"{total:.3f}", str(out)]
    log("… الإخراج النهائي")
    run(cmd)

    # ---- مخرجات مرافقة
    write_srt(cues, out_dir / f"{ep.get('slug', 'episode')}_ar.srt")
    report = []
    for s, r in zip(report_shots, report_res):
        L = r["license"]
        report.append({"shot": s["id"], "start": round(s["start"], 2), "end": round(s["end"], 2),
                       "file": r["path"].name, "source": L.get("source"), "url": L.get("url"),
                       "author": L.get("author"), "license": L.get("license") or
                       LICENSES.get(L.get("source", "").lower(), {}).get("name"),
                       "needs_review": bool(L.get("needs_review"))})
    for amb in amb_records:
        report.append({"audio": amb["file"], "source": amb.get("source"), "url": amb.get("url"),
                       "author": amb.get("author"), "license": amb.get("license")})
    (out_dir / "licenses_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
    credits = credits_text(report, a)
    (out_dir / "credits_for_description.txt").write_text(credits, encoding="utf-8")
    firsts = [(sf, sh) for sf, sh in zip(seg_files, shots) if not sh.get("_part")]
    contact_sheet([x[0] for x in firsts], [x[1] for x in firsts], out_dir / "contact_sheet.jpg")
    verify(out, total)
    if test_only:
        log("⚠ هذا إخراج اختباري بمقاطع اختبار — لا يُنشر.")
    if any(x.get("needs_review") for x in report):
        log("⚠ مقاطع جُلبت آلياً: راجع contact_sheet.jpg (لا وجوه، لا ما يخالف الضوابط) قبل النشر.")
    log(f"✔ {out}")


def credits_text(report, audio):
    lines, seen = [], set()
    for r in report:
        src = (r.get("source") or "").lower()
        if src in ("own", "test", "") or not r.get("url"):
            continue
        key = r["url"]
        if key in seen:
            continue
        seen.add(key)
        who = f" — {r['author']}" if r.get("author") else ""
        lines.append(f"• {r.get('source')}{who}: {r['url']}")
    out = ["المصادر البصرية والصوتية:"] + (lines or ["• تصوير خاص"])
    if audio.get("credit"):
        out.append(audio["credit"])
    return "\n".join(out) + "\n"


def contact_sheet(seg_files, shots, path):
    tmp = path.parent / "_cs"
    tmp.mkdir(exist_ok=True)
    frames = []
    for i, ((seg, L), s) in enumerate(zip(seg_files, shots)):
        f = tmp / f"{i:02d}.jpg"
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{L / 2:.2f}", "-i", str(seg),
             "-frames:v", "1", "-vf", "scale=270:480", str(f)])
        frames.append(f)
    cols = 7
    rows = (len(frames) + cols - 1) // cols
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-framerate", "1", "-i", str(tmp / "%02d.jpg"),
         "-vf", f"tile={cols}x{rows}:padding=6:color=black", "-frames:v", "1", str(path)])
    shutil.rmtree(tmp, ignore_errors=True)


def verify(out, total):
    j = probe(out)
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    au = next(s for s in j["streams"] if s["codec_type"] == "audio")
    dur = float(j["format"]["duration"])
    fps = v["r_frame_rate"]
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(out), "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    I = re.findall(r"I:\s+(-?[\d.]+) LUFS", r)
    P = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", r)
    checks = [
        ("الأبعاد 1080×1920", v["width"] == W and v["height"] == H),
        ("ترميز H.264 / yuv420p", v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"),
        ("30 إطاراً/ث", fps in ("30/1", "30000/1000")),
        (f"المدة {dur:.2f} ث (المطلوب {total:.2f})", abs(dur - total) < 0.1),
        ("صوت AAC 48kHz", au["codec_name"] == "aac" and au["sample_rate"] == "48000"),
        (f"الجهارة {I[-1] if I else '?'} LUFS (هدف -14)", bool(I) and abs(float(I[-1]) + 14) <= 1.5),
        (f"الذروة {P[-1] if P else '?'} dBFS", bool(P) and float(P[-1]) <= -0.5),
        (f"المدة ضمن {MIN_DUR:.0f}–{MAX_DUR:.0f} ث", MIN_DUR <= dur <= MAX_DUR),
    ]
    log("الفحص:")
    for name, ok in checks:
        log(f"  {'✔' if ok else '✖'} {name}")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="خط إنتاج الفيديوهات القصيرة")
    ap.add_argument("command", choices=["doctor", "check", "fetch", "tts", "align", "patch", "render", "all"])
    ap.add_argument("episode", nargs="?")
    ap.add_argument("--best", action="store_true", help="ترميز أبطأ بجودة أعلى")
    ap.add_argument("--words", help="ملف JSON بتوقيت الكلمات (بديل عن التفريغ)")
    ap.add_argument("--at", nargs=2, type=float, metavar=("START", "END"), help="patch: موضع العبارة بالثواني")
    ap.add_argument("--with", dest="with_file", help="patch: ملف العبارة المصحَّحة")
    args = ap.parse_args()
    if args.command == "patch":
        if not (args.episode and args.at and args.with_file):
            die("الاستخدام: patch EPISODE --at START END --with FILE")
        return cmd_patch(load_episode(args.episode), args.at[0], args.at[1], Path(args.with_file))
    if args.command == "doctor":
        return cmd_doctor()
    if not args.episode:
        die("حدد مجلد الحلقة")
    ep = load_episode(args.episode)
    if args.command == "all":                       # من البداية إلى النهاية
        if ep["audio"].get("from_tts"):
            cmd_tts(ep)
        cmd_fetch(ep)
        return cmd_render(load_episode(args.episode), args)
    {"check": lambda: cmd_check(ep), "fetch": lambda: cmd_fetch(ep), "tts": lambda: cmd_tts(ep),
     "align": lambda: cmd_align(ep, args.words), "render": lambda: cmd_render(ep, args)}[args.command]()


if __name__ == "__main__":
    main()
