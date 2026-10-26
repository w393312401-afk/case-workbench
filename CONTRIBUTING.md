# 参与开发

先按 [README](README.md) 安装依赖。在独立目录和虚构数据上复现问题，提交 Issue 时写清操作步骤、预期结果、实际结果与运行环境。

不要提交 `config.local.json`、API 密钥、Cookie、远程登录凭据、真实评论、销售报表、客户资料、案例库或媒体文件。截图也应使用虚构数据。

修改数据结构时保留旧记录的读取能力；修改写入逻辑时使用临时目录测试。基础检查：

```powershell
python -m unittest discover -s tests -p test_app_config.py
python -m unittest discover -s tests -p test_review_store.py
python -m unittest discover -s tests -p test_review_opportunities.py
python -m unittest discover -s tests -p test_server.py
```

完整测试还包含浏览器、图像与外部工具相关用例，需要相应环境；不要把未运行的检查记为通过。提交改动时说明影响范围、验证方式和已知限制。
