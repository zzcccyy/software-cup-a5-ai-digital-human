from __future__ import annotations

import hashlib
import re
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


BUNDLE_DIRNAME = "示范景区公开资料包"
MAX_CHUNK_LENGTH = 640
STRUCTURED_SPOT_RE = re.compile(r"(灵山胜境\s+LS-\d{3}\s+[\u4e00-\u9fffA-Za-z0-9\s]+)")


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _split_semantic_chunks(text: str, max_len: int = MAX_CHUNK_LENGTH) -> list[dict]:
    sections = re.split(r"(?=\n{2,}|(?<=\n)##|(?<=\n)###|(?<=\n)【)", text)
    chunks: list[dict] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        sentences = re.split(r"(?<=[。！？；])", section)
        current = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(current) + len(s) <= max_len:
                current += s
            else:
                if current:
                    chunks.append({"content": current.strip(), "type": "semantic"})
                current = s
        if current:
            chunks.append({"content": current.strip(), "type": "semantic"})
    return chunks


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml_data = archive.read("word/document.xml")
    root = ET.fromstring(xml_data)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    heading_level = 0
    for paragraph in root.findall(".//w:p", ns):
        texts = [node.text or "" for node in paragraph.findall(".//w:t", ns)]
        line = _clean_text("".join(texts))
        if not line:
            continue
        ppr = paragraph.find(".//w:pPr", ns)
        if ppr is not None:
            outline = ppr.find(".//w:outlineLvl", ns)
            if outline is not None:
                try:
                    heading_level = int(outline.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "0"))
                except (ValueError, TypeError):
                    heading_level = 0
                line = f"{'#' * (heading_level + 1)} {line}"
        paragraphs.append(line)
    return "\n".join(paragraphs)


def _structured_docx_chunks(text: str) -> list[dict]:
    text = _clean_text(text)
    if "灵山胜境 LS-" not in text:
        return []
    parts = STRUCTURED_SPOT_RE.split(text)
    chunks: list[dict] = []
    pending_header = ""
    for part in parts:
        part = _clean_text(part)
        if not part:
            continue
        if STRUCTURED_SPOT_RE.fullmatch(part):
            pending_header = part
            continue
        if pending_header:
            chunk_text = _clean_text(f"{pending_header} {part}")
            pending_header = ""
            if len(chunk_text) > 80:
                spot_name_match = re.search(r"LS-\d{3}\s+([\u4e00-\u9fffA-Za-z0-9]+)", chunk_text)
                spot_name = spot_name_match.group(1) if spot_name_match else ""
                fact_types = _extract_fact_types(chunk_text)
                chunks.append({
                    "content": chunk_text,
                    "type": "structured",
                    "spot_name": spot_name,
                    "fact_types": fact_types,
                })
    cleaned_chunks: list[dict] = []
    for chunk in chunks:
        text_content = chunk["content"]
        text_content = re.sub(r"数据集说明.+?字段规范.+?子表：", "", text_content)
        text_content = re.sub(r"景区名称\s+景点ID\s+景点名称\s+具体位置.+?备注", "", text_content)
        text_content = _clean_text(text_content)
        if text_content:
            chunk["content"] = text_content
            cleaned_chunks.append(chunk)
    return cleaned_chunks


def _extract_fact_types(text: str) -> list[str]:
    types = []
    if re.search(r"(高度|通高|高\s*\d+|\d+\.?\d*\s*米)", text):
        types.append("height")
    if re.search(r"(位于|坐落|地址|位置|在哪)", text):
        types.append("location")
    if re.search(r"(门票|票价|价格|收费|免费|元/人|元)", text):
        types.append("ticket")
    if re.search(r"(开放时间|营业时间|几点|开园|闭园|演出时间)", text):
        types.append("opening_hours")
    if re.search(r"(历史|建于|始建于|创建|起源|传说|典故|文化)", text):
        types.append("history")
    if re.search(r"(特色|亮点|看点|值得)", text):
        types.append("feature")
    return types


def _split_by_headings(text: str) -> list[dict]:
    chunks: list[dict] = []
    lines = text.split("\n")
    current_heading = ""
    current_content = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if current_content.strip():
                chunks.append({
                    "content": current_content.strip(),
                    "type": "heading",
                    "heading": current_heading,
                })
            current_heading = stripped.lstrip("#").strip()
            current_content = ""
        else:
            current_content += stripped + "\n"
    if current_content.strip():
        chunks.append({
            "content": current_content.strip(),
            "type": "heading",
            "heading": current_heading,
        })
    return chunks


def _guess_category(text: str) -> str:
    text_lower = text.lower()
    if any(token in text_lower for token in ["路线", "游览", "推荐", "怎么逛", "怎么玩"]):
        return "路线资料"
    if any(token in text_lower for token in ["停车", "卫生间", "游客中心", "服务", "轮椅", "餐饮", "美食", "休息"]):
        return "服务信息"
    if any(token in text_lower for token in ["历史", "文化", "建筑", "佛教", "传说", "典故", "始建于"]):
        return "文化资料"
    if any(token in text_lower for token in ["自然", "风景", "山水", "风光", "拍照"]):
        return "自然风光"
    if any(token in text_lower for token in ["高度", "门票", "票价", "开放时间", "位于", "地址"]):
        return "景区信息"
    return "景区资料"


def _tags_for(text: str) -> list[str]:
    candidates = [
        "灵山大佛", "梵宫", "九龙灌浴", "五印坛城", "祥符禅寺",
        "佛足坛", "五明桥", "五智门", "无尽意斋", "拈花广场",
        "梵天花海", "香月花街", "拈花堂", "五灯湖", "鹿鸣谷",
        "游客中心", "湖景步道", "观景平台", "静心休憩区", "文化商店",
        "历史", "文化", "佛教", "自然", "亲子", "服务",
    ]
    return [tag for tag in candidates if tag in text][:5]


def _title_for(file_name: str, chunk: dict, chunk_index: int) -> str:
    spot_match = re.search(r"LS-\d{3}\s+([\u4e00-\u9fffA-Za-z0-9]+)", chunk.get("content", ""))
    if spot_match:
        return f"{file_name} - {spot_match.group(1)}"
    if chunk.get("heading"):
        return f"{file_name} - {chunk['heading']}"
    if chunk.get("spot_name"):
        return f"{file_name} - {chunk['spot_name']}"
    first_sentence = re.split(r"[。\n！？]", chunk.get("content", ""))[0][:28].strip()
    if first_sentence:
        return f"{file_name} - {first_sentence}"
    return f"{file_name} - 资料片段 {chunk_index + 1}"


def build_bundle_knowledge(base_dir: Path) -> list[dict]:
    bundle_dir = base_dir / BUNDLE_DIRNAME
    if not bundle_dir.exists():
        bundle_dir = base_dir / "data" / BUNDLE_DIRNAME
    if not bundle_dir.exists():
        return []

    items: list[dict] = []
    errors: list[str] = []
    for path in sorted(bundle_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".docx",):
            continue

        try:
            raw_text = _docx_text(path)
        except (zipfile.BadZipFile, ET.ParseError, KeyError) as e:
            errors.append(f"[warn] 跳过文件 {path.name}: DOCX解析失败 - {e}")
            continue
        except Exception as e:
            errors.append(f"[warn] 跳过文件 {path.name}: 未知错误 - {e}")
            continue

        cleaned = _clean_text(raw_text)
        if not cleaned:
            continue

        try:
            structured_chunks = _structured_docx_chunks(cleaned)
            if structured_chunks:
                chunks = structured_chunks
            elif "##" in cleaned or "###" in cleaned:
                heading_chunks = _split_by_headings(cleaned)
                if heading_chunks:
                    chunks = heading_chunks
                else:
                    chunks = _split_semantic_chunks(cleaned)
            else:
                chunks = _split_semantic_chunks(cleaned)
        except Exception as e:
            errors.append(f"[warn] 跳过文件 {path.name}: 分块失败 - {e}")
            continue

        for index, chunk in enumerate(chunks):
            try:
                content = chunk.get("content", "")
                if len(content) < 20:
                    continue
                content_hash = hashlib.sha1(f"{path.name}:{content}".encode("utf-8")).hexdigest()[:16]
                fact_types = chunk.get("fact_types", _extract_fact_types(content))
                items.append({
                    "id": str(uuid.uuid4()),
                    "title": _title_for(path.stem, chunk, index),
                    "category": _guess_category(content),
                    "tags": _tags_for(content),
                    "content": content,
                    "source": f"{BUNDLE_DIRNAME}/{path.name}",
                    "source_hash": content_hash,
                    "fact_types": fact_types,
                    "spot_name": chunk.get("spot_name", ""),
                    "chunk_type": chunk.get("type", "semantic"),
                })
            except Exception as e:
                errors.append(f"[warn] 文件 {path.name} 第{index+1}条分块处理失败: {e}")
                continue

    if errors:
        print(f"[bundle_importer] 导入完成: 成功 {len(items)} 条, 错误 {len(errors)} 条")
        for err in errors:
            print(err)
    else:
        print(f"[bundle_importer] 导入完成: 成功 {len(items)} 条")
    return items
