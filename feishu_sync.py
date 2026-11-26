"""上传 Notion 成功后，把同一条成品同步写进飞书多维表格「TK 视频内容管理库 / 视频内容库」（2026-11-24 fly 定）。

通过剪映自带的飞书 CLI（lark-cli，用户身份）读写，不存任何密钥。按「视频 ID / 编号」找记录：先找新名，
找不到再找旧名（如 Zack261124-202-1），找到就更新那条并把编号改成新名，都没有才新建；工作台记下 record_id，
重传时直接更新同一条。附件整格替换：先传新文件，再删掉原来的文件。已有记录的「视频状态」不动，新建记为「就绪」；
发布账号、产品型号只填表里已有的选项，不新增选项。

飞书只是旁路：找不到 CLI 就跳过，失败只记在上传记录的 feishu_* 字段，不影响 Notion 结果。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

TABLE_FIELDS = {"name": "视频 ID / 编号", "title": "视频标题 + Hashtag", "day": "发布日期", "account": "发布账号",
                "product": "产品型号", "copy": "脚本文案", "status": "视频状态", "files": "成片视频"}
STATUS_READY = "就绪"
_CLI_GLOB = "*/Resources/agent_runtime_env/cli/npm-tools/node_modules/@larksuite/cli/bin/lark-cli.exe"
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class FeishuError(Exception):
    pass


def _version_key(text):
    return tuple(int(part) for part in re.findall(r"\d+", text))


def find_cli(configured=""):
    """配置里写了就用它；否则在剪映各版本目录里挑版本号最高的 lark-cli（剪映升级会换路径），再退到 PATH。"""
    if configured and Path(configured).is_file():
        return str(Path(configured))
    apps = Path(os.environ.get("LOCALAPPDATA", "")) / "JianyingPro" / "Apps"
    found = []
    try:
        found = [path for path in apps.glob(_CLI_GLOB) if path.is_file()]
    except OSError:
        pass
    if found:
        return str(max(found, key=lambda path: _version_key(path.relative_to(apps).parts[0])))
    return shutil.which("lark-cli") or ""


def _strip(value):
    return " ".join(str(value or "").split())


class FeishuSync:
    def __init__(self, settings, runner=None, cli_finder=find_cli):
        self.settings = dict(settings or {})
        self.runner = runner or subprocess.run
        self.cli_finder = cli_finder

    @property
    def enabled(self):
        return bool(self.settings.get("base_token") and self.settings.get("table_id"))

    # ---------- lark-cli ----------
    def _call(self, cli, args, cwd=None, timeout=120):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        command = [cli, "base", *args, "--base-token", self.settings["base_token"],
                   "--table-id", self.settings["table_id"], "--as", "user"]
        try:
            done = self.runner(command, capture_output=True, timeout=timeout, cwd=cwd, creationflags=flags)
        except subprocess.TimeoutExpired:
            raise FeishuError("飞书 CLI 超时没有回应。") from None
        except OSError as exc:
            raise FeishuError(f"飞书 CLI 无法运行：{exc}") from None
        out = (done.stdout or b"").decode("utf-8", "replace")
        start = out.find("{")
        try:
            data = json.loads(out[start:]) if start >= 0 else {}
        except ValueError:
            data = {}
        if done.returncode != 0 or data.get("ok") is False or not data:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            detail = error.get("message") or data.get("msg") or (done.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            raise FeishuError("飞书 CLI 报错：" + (detail or f"退出码 {done.returncode}"))
        return data.get("data") or {}

    @staticmethod
    def _rows(data):
        """record-list / record-get 的 json 矩阵 → [{record_id, 字段名: 值}]。"""
        fields, rows = data.get("fields") or [], data.get("data") or []
        return [{"record_id": rid, **dict(zip(fields, row))} for rid, row in zip(data.get("record_id_list") or [], rows)]

    @staticmethod
    def _record_id(data):
        if isinstance(data, dict):
            for key in ("record_id", "id"):
                if isinstance(data.get(key), str) and data[key].startswith("rec"):
                    return data[key]
            if data.get("record_id_list"):
                return data["record_id_list"][0]
            for value in data.values():
                found = FeishuSync._record_id(value)
                if found:
                    return found
        if isinstance(data, list):
            for value in data:
                found = FeishuSync._record_id(value)
                if found:
                    return found
        return ""

    # ---------- 找记录 ----------
    def _options(self, cli):
        fields = self._call(cli, ["+field-list"]).get("fields") or []
        by_name = {field.get("name"): field for field in fields}
        missing = [name for name in TABLE_FIELDS.values() if name not in by_name]
        if missing:
            raise FeishuError("飞书表里缺少字段：" + "、".join(missing))
        return {key: [option.get("name") for option in by_name[TABLE_FIELDS[key]].get("options") or []]
                for key in ("account", "product", "status")}

    def _get(self, cli, record_id):
        try:
            rows = self._rows(self._call(cli, ["+record-get", "--record-id", record_id, "--field-id", TABLE_FIELDS["name"],
                                               "--field-id", TABLE_FIELDS["files"], "--format", "json"]))
        except FeishuError:
            return None  # 记录被人删了：退回按名字找
        return rows[0] if rows else None

    def _find(self, cli, names):
        names = [name for name in dict.fromkeys(_strip(name) for name in names) if name]
        query = {"logic": "or", "conditions": [[TABLE_FIELDS["name"], "==", name] for name in names]}
        rows = self._rows(self._call(cli, ["+record-list", "--filter-json", json.dumps(query, ensure_ascii=False),
                                           "--field-id", TABLE_FIELDS["name"], "--field-id", TABLE_FIELDS["files"],
                                           "--format", "json", "--limit", "20"]))
        for name in names:  # 新名优先，再旧名
            hits = [row for row in rows if _strip(row.get(TABLE_FIELDS["name"])) == name]
            if len(hits) > 1:
                raise FeishuError(f"飞书里有 {len(hits)} 条「{name}」，请先只留一条再上传。")
            if hits:
                return hits[0]
        return None

    # ---------- 写 ----------
    def account_for(self, kind, creator, account, options):
        """配置的映射（按达人或上传填的账号，不分大小写）→ 上传填的账号原样；不在已有选项里就不填。"""
        mapping = (self.settings.get("accounts") or {}).get(kind) or {}
        lowered = {str(key).casefold(): value for key, value in mapping.items()}
        for key in (creator, account, str(account or "").lstrip("@")):
            if key and str(key).casefold() in lowered:
                return lowered[str(key).casefold()], ""
        wanted = str(account or "").strip()
        match = next((option for option in options if option.casefold() == wanted.casefold()), "")
        if match:
            return match, ""
        return "", f"飞书「发布账号」没有「{wanted or creator}」这个选项，这一格没填。"

    def sync(self, kind, row, files, creator="", product="", on_record=None):
        """kind: video / photo；row: 上传记录（name、legacy_name、title、published_on、account、source、feishu_record_id）；
        files: [(本地路径, 上传后的文件名)]。返回 {record_id, warning}；CLI 找不到时 status=skipped。"""
        cli = self.cli_finder(self.settings.get("cli", ""))
        if not cli:
            return {"status": "skipped", "record_id": row.get("feishu_record_id", ""),
                    "warning": "没找到飞书 CLI（剪映自带的 lark-cli），这次没同步飞书。"}
        options = self._options(cli)
        warnings = []
        record = self._get(cli, row["feishu_record_id"]) if row.get("feishu_record_id") else None
        if record is None:
            record = self._find(cli, [row["name"], row.get("legacy_name", "")])
        source = row.get("source") or {}
        texts = [source.get("en"), source.get("zh")] if kind == "video" else [source.get("caption_en"), source.get("caption_zh")]
        fields = {TABLE_FIELDS["name"]: row["name"], TABLE_FIELDS["title"]: row.get("title", ""),
                  TABLE_FIELDS["day"]: row["published_on"], TABLE_FIELDS["copy"]: "\n\n".join(filter(None, texts))}
        account, problem = self.account_for(kind, creator, row.get("account", ""), options["account"])
        if account:
            fields[TABLE_FIELDS["account"]] = [account]
        if problem:
            warnings.append(problem)
        if product and product in options["product"]:
            fields[TABLE_FIELDS["product"]] = [product]
        if record:
            record_id = record["record_id"]
            self._call(cli, ["+record-upsert", "--record-id", record_id, "--json", json.dumps(fields, ensure_ascii=False)])
        else:
            if STATUS_READY in options["status"]:
                fields[TABLE_FIELDS["status"]] = [STATUS_READY]
            record_id = self._record_id(self._call(cli, ["+record-upsert", "--json", json.dumps(fields, ensure_ascii=False)]))
            if not record_id:
                raise FeishuError("飞书新建记录后没拿到记录编号，重试会先按名字找回。")
        if on_record:
            on_record(record_id)
        old = [item.get("file_token") for item in (record or {}).get(TABLE_FIELDS["files"]) or [] if item.get("file_token")]
        if files:
            self._replace_files(cli, record_id, files, old)
        return {"status": "synced", "record_id": record_id, "warning": "；".join(warnings)}

    def _replace_files(self, cli, record_id, files, old):
        """先传新的（附加到格子里），成功后再删原来的：中途失败时旧文件还在。"""
        with tempfile.TemporaryDirectory(prefix="feishu-upload-") as staging:
            names = []
            for path, name in files:
                name = _UNSAFE.sub("_", name)
                target = Path(staging) / name
                try:
                    os.link(path, target)
                except OSError:
                    shutil.copyfile(path, target)
                names.append(name)
            for start in range(0, len(names), 50):  # CLI 一次最多 50 个
                args = ["+record-upload-attachment", "--record-id", record_id, "--field-id", TABLE_FIELDS["files"]]
                for name in names[start:start + 50]:
                    args += ["--file", name]
                self._call(cli, args, cwd=staging, timeout=900)
        if old:
            args = ["+record-remove-attachment", "--record-id", record_id, "--field-id", TABLE_FIELDS["files"], "--yes"]
            for token in old:
                args += ["--file-token", token]
            self._call(cli, args)
