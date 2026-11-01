# 案例工作台

动手前先读 [docs/项目地图.md](docs/项目地图.md)，按业务线找文件。新增、删除或改名代码文件后，同步更新地图并运行：

```bash
python scripts/check_project_map.py
```

## 重启

- 后端改动要重启才生效，前端文件刷新页面即可。
- 重启命令：`python -c "import launcher_control as l; raise SystemExit(l.main(['--no-browser']))"`。直接运行 `launcher_control.py` 不会有任何效果。
- 每次重启都会占用一个备份名额（保留最新 14 份）。重启前先确认运行目录的数据完好，否则空备份会挤掉旧的完整备份。

## 测试

- 用 `python -m pytest tests/test_xxx.py` 跑相关测试，需要把 `PYTHONPATH` 设为项目根目录。
- 不要动真实数据：测试只用临时目录，排查问题时也不要往运行目录写测试数据。

## 不要移走

`archive/`、`图文素材/`、`图文成品/`、`图文提示词/`、`reports/`、`deployment/`。原因见地图的「数据在哪里」和「根目录其他内容」。历史资料放在代码目录外的 `case-workbench-资料/`。
