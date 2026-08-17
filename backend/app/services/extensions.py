from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from langchain_core.tools import BaseTool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import ExtensionPackage

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_PACKAGE_BYTES = 20 * 1024 * 1024
MAX_PACKAGE_FILES = 500
MAX_SKILL_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class ToolContext:
    requester_id: str
    conversation_id: str | None
    platform: str
    is_group: bool
    workspace_path: Path
    is_owner: bool = False


class ExtensionError(ValueError):
    pass


def _validate_name(name: object) -> str:
    value = str(name or "")
    if not NAME_PATTERN.fullmatch(value) or len(value) > 64:
        raise ExtensionError("扩展名称必须是小写字母、数字和单连字符，长度不超过 64")
    return value


def _safe_zip_member(info: zipfile.ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    if not path.parts or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ExtensionError(f"ZIP 包含不安全路径: {info.filename}")
    file_type = (info.external_attr >> 16) & 0o170000
    if file_type == 0o120000:
        raise ExtensionError("ZIP 不允许包含符号链接")


def _extract_zip(content: bytes, target: Path) -> Path:
    if len(content) > MAX_PACKAGE_BYTES:
        raise ExtensionError("扩展包不能超过 20 MiB")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = archive.infolist()
        if len(members) > MAX_PACKAGE_FILES:
            raise ExtensionError("扩展包文件数不能超过 500")
        for info in members:
            _safe_zip_member(info)
        archive.extractall(target)
    roots = [item for item in target.iterdir()]
    if len(roots) == 1 and roots[0].is_dir():
        return roots[0]
    return target


def _sha256_directory(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def _parse_skill_frontmatter(text: str) -> dict[str, object]:
    if not text.startswith("---\n"):
        raise ExtensionError("SKILL.md 必须以 YAML frontmatter 开始")
    marker = text.find("\n---", 4)
    if marker < 0:
        raise ExtensionError("SKILL.md 缺少 frontmatter 结束标记")
    result: dict[str, object] = {}
    for line in text[4:marker].splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip("\"'")
    name = _validate_name(result.get("name"))
    description = str(result.get("description") or "").strip()
    if not description or len(description) > 1024:
        raise ExtensionError("SKILL.md description 必须为 1-1024 字符")
    result["name"] = name
    result["description"] = description
    return result


def _validate_tool_manifest(root: Path) -> dict[str, object]:
    path = root / "tool.json"
    if not path.is_file():
        raise ExtensionError("Tool 包缺少 tool.json")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExtensionError("tool.json 不是有效 JSON") from error
    if not isinstance(manifest, dict):
        raise ExtensionError("tool.json 必须是对象")
    manifest["name"] = _validate_name(manifest.get("name"))
    if not str(manifest.get("description") or "").strip():
        raise ExtensionError("Tool 必须填写 description")
    entrypoint = str(manifest.get("entrypoint") or "")
    if ":" not in entrypoint:
        raise ExtensionError("Tool entrypoint 必须是 module.py:create_tools")
    module_name, function_name = entrypoint.split(":", 1)
    module_path = PurePosixPath(module_name)
    if (
        module_path.is_absolute()
        or ".." in module_path.parts
        or not (root / Path(*module_path.parts)).is_file()
        or function_name != "create_tools"
    ):
        raise ExtensionError("Tool entrypoint 文件或 create_tools 不存在")
    manifest["entrypoint"] = entrypoint
    manifest["permissions"] = manifest.get("permissions") or []
    manifest["requires"] = manifest.get("requires") or []
    return manifest


def _validate_skill(root: Path) -> dict[str, object]:
    path = root / "SKILL.md"
    if not path.is_file():
        raise ExtensionError("Skill 包缺少 SKILL.md")
    text = path.read_text(encoding="utf-8-sig")
    if len(text) > MAX_SKILL_CHARS:
        raise ExtensionError("SKILL.md 不能超过 20,000 字符")
    manifest = _parse_skill_frontmatter(text)
    if root.name != str(manifest["name"]):
        raise ExtensionError("Skill 目录名必须与 frontmatter name 一致")
    return manifest


def import_zip(
    session: Session,
    kind: str,
    content: bytes,
    filename: str,
    source_type: str = "local",
    source_url: str | None = None,
    source_ref: str | None = None,
) -> ExtensionPackage:
    if kind not in {"tool", "skill"}:
        raise ExtensionError("kind 只能是 tool 或 skill")
    settings = get_settings()
    destination: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="personal-agent-extension-") as temporary:
            root = _extract_zip(content, Path(temporary))
            manifest = _validate_tool_manifest(root) if kind == "tool" else _validate_skill(root)
            name = str(manifest["name"])
            existing = session.scalar(
                select(ExtensionPackage).where(
                    ExtensionPackage.kind == kind, ExtensionPackage.name == name
                )
            )
            if existing is not None:
                raise ExtensionError(f"扩展已存在: {kind}/{name}")
            destination_root = settings.tools_path if kind == "tool" else settings.skills_path
            destination = destination_root / name
            if destination.exists():
                raise ExtensionError(f"扩展目录已存在: {destination}")
            shutil.copytree(root, destination)
        item = ExtensionPackage(
            kind=kind,
            name=name,
            version=str(manifest.get("version") or "0.0.0"),
            description=str(manifest.get("description") or ""),
            source_type=source_type,
            source_url=source_url,
            source_ref=source_ref,
            sha256=_sha256_directory(destination),
            install_path=str(destination.resolve()),
            manifest=json.dumps(manifest, ensure_ascii=False),
            permissions=json.dumps(manifest.get("permissions") or [], ensure_ascii=False),
            status="installed_disabled",
            enabled=False,
            builtin=False,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return item
    except Exception:
        if destination is not None and destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise


def ensure_builtin_packages(session: Session) -> None:
    builtins = [
        ("tool", "knowledge-search", "在知识库中进行混合检索", "knowledge"),
        ("tool", "manga", "搜索、确认和下载漫画", "manga"),
        ("tool", "create-files", "在用户隔离目录创建文件", "files"),
    ]
    for kind, name, description, marker in builtins:
        item = session.scalar(
            select(ExtensionPackage).where(ExtensionPackage.kind == kind, ExtensionPackage.name == name)
        )
        if item is None:
            session.add(
                ExtensionPackage(
                    kind=kind,
                    name=name,
                    version="builtin",
                    description=description,
                    source_type="builtin",
                    sha256=f"builtin:{marker}",
                    install_path="",
                    manifest=json.dumps({"name": name, "description": description}, ensure_ascii=False),
                    permissions="[]",
                    access_policy="private_users",
                    status="ready",
                    enabled=True,
                    builtin=True,
                )
            )
    session.commit()


def load_python_tools(session: Session, context: ToolContext) -> list[BaseTool]:
    tools: list[BaseTool] = []
    rows = session.scalars(
        select(ExtensionPackage).where(
            ExtensionPackage.kind == "tool",
            ExtensionPackage.enabled.is_(True),
            ExtensionPackage.builtin.is_(False),
        )
    ).all()
    for item in rows:
        if item.access_policy == "owner_only" and not context.is_owner:
            continue
        if context.is_group:
            continue
        try:
            manifest = json.loads(item.manifest)
            module_name, function_name = str(manifest["entrypoint"]).split(":", 1)
            module_relative = PurePosixPath(module_name)
            if module_relative.is_absolute() or ".." in module_relative.parts:
                raise ExtensionError("Tool entrypoint 路径无效")
            module_path = Path(item.install_path) / Path(*module_relative.parts)
            unique_name = f"personal_agent_tool_{item.name}_{uuid.uuid4().hex}"
            spec = importlib.util.spec_from_file_location(unique_name, module_path)
            if spec is None or spec.loader is None:
                raise ExtensionError("无法加载 Tool 模块")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            factory = getattr(module, function_name)
            created = factory(context)
            if not isinstance(created, list) or not all(isinstance(tool, BaseTool) for tool in created):
                raise ExtensionError("create_tools 必须返回 BaseTool 列表")
            tools.extend(created)
        except Exception as error:
            item.status = "error"
            item.error = f"{type(error).__name__}: {error}"
    session.commit()
    return tools


def list_packages(session: Session, kind: str | None = None) -> list[ExtensionPackage]:
    query = select(ExtensionPackage).order_by(ExtensionPackage.kind, ExtensionPackage.name)
    if kind:
        query = query.where(ExtensionPackage.kind == kind)
    return list(session.scalars(query))


def load_skill_text(session: Session, name: str) -> str:
    item = session.scalar(
        select(ExtensionPackage).where(
            ExtensionPackage.kind == "skill",
            ExtensionPackage.name == name,
            ExtensionPackage.enabled.is_(True),
        )
    )
    if item is None:
        raise ExtensionError(f"Skill 不存在或未启用: {name}")
    path = Path(item.install_path).resolve() / "SKILL.md"
    root = Path(item.install_path).resolve()
    if root not in path.parents or not path.is_file():
        raise ExtensionError("Skill 文件路径无效")
    text = path.read_text(encoding="utf-8-sig")
    if len(text) > MAX_SKILL_CHARS:
        raise ExtensionError("SKILL.md 不能超过 20,000 字符")
    return text


def read_skill_resource(session: Session, name: str, relative_path: str) -> str:
    item = session.scalar(
        select(ExtensionPackage).where(
            ExtensionPackage.kind == "skill",
            ExtensionPackage.name == name,
            ExtensionPackage.enabled.is_(True),
        )
    )
    if item is None:
        raise ExtensionError(f"Skill 不存在或未启用: {name}")
    root = Path(item.install_path).resolve()
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ExtensionError("Skill resource 路径无效")
    candidate = (root / Path(*relative.parts)).resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise ExtensionError("Skill resource 不存在或越界")
    if candidate.name.lower() == "skill.md":
        raise ExtensionError("请使用 load_skill 读取 SKILL.md")
    return candidate.read_text(encoding="utf-8-sig")


def github_zip_url(url: str) -> tuple[str, str, tuple[str, ...]]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise ExtensionError("只允许 HTTPS 的 github.com 公共仓库地址")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] in {"..", "."} or parts[1] in {"..", "."}:
        raise ExtensionError("GitHub 地址必须包含 owner/repository")
    owner, repository = parts[0], parts[1].removesuffix(".git")
    if not NAME_PATTERN.fullmatch(owner.lower()) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
        raise ExtensionError("GitHub owner/repository 地址无效")
    ref = "main"
    subpath: tuple[str, ...] = ()
    if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
        ref = parts[3] or "main"
        subpath = tuple(parts[4:]) if parts[2] == "tree" else tuple(parts[4:-1])
        if ".." in PurePosixPath(ref).parts or ".." in subpath:
            raise ExtensionError("GitHub ref 无效")
    download_url = f"https://codeload.github.com/{owner}/{repository}/zip/refs/heads/{ref}"
    return download_url, f"{owner}/{repository}@{ref}", subpath


def import_github(
    session: Session,
    kind: str,
    url: str,
    *,
    timeout: float = 30.0,
) -> ExtensionPackage:
    import httpx

    download_url, source_ref, subpath = github_zip_url(url)
    response = httpx.get(download_url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    content = response.content
    if subpath:
        with tempfile.TemporaryDirectory(prefix="personal-agent-github-") as temporary:
            root = _extract_zip(content, Path(temporary))
            selected = (root / Path(*subpath)).resolve()
            root_resolved = root.resolve()
            if root_resolved not in selected.parents or not selected.is_dir():
                raise ExtensionError("GitHub 子目录不存在或越界")
            packed = io.BytesIO()
            with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in selected.rglob("*"):
                    if path.is_file():
                        archive.write(
                            path,
                            f"{selected.name}/{path.relative_to(selected).as_posix()}",
                        )
            content = packed.getvalue()
    return import_zip(
        session,
        kind,
        content,
        f"{source_ref.replace('/', '-')}.zip",
        source_type="github",
        source_url=url,
        source_ref=source_ref,
    )


def package_dict(item: ExtensionPackage) -> dict[str, object]:
    try:
        manifest = json.loads(item.manifest or "{}")
    except json.JSONDecodeError:
        manifest = {}
    dependencies = []
    for requirement in manifest.get("requires", []) if isinstance(manifest, dict) else []:
        name = str(requirement).split("==", 1)[0].split(">=", 1)[0].strip()
        dependencies.append({"name": name, "available": bool(name and importlib.util.find_spec(name))})
    return {
        "id": item.id,
        "kind": item.kind,
        "name": item.name,
        "version": item.version,
        "description": item.description,
        "source_type": item.source_type,
        "source_url": item.source_url,
        "source_ref": item.source_ref,
        "sha256": item.sha256,
        "permissions": json.loads(item.permissions or "[]"),
        "dependency_check": dependencies,
        "access_policy": item.access_policy,
        "status": item.status,
        "enabled": item.enabled,
        "builtin": item.builtin,
        "error": item.error,
    }
