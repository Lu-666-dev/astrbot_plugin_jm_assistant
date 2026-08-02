# 机长小助手

AstrBot QQ 插件。私聊或按群聊白名单/黑名单模式允许的群消息中出现 `JM` 加六位数字时，插件会将它视为 JMComic 作品 ID，下载对应作品的全部图片并生成一个 PDF 文件发送。

## 使用方式

在 QQ 私聊中，或已配置的 QQ 群中发送包含 `JM` 加六位数字的消息，例如：

```text
JM350234
```

`JM` 前缀不区分大小写，单独发送 `350234` 不会触发。插件默认只取消息中的第一个合法车号；七位或更长的连续数字不会被误识别为六位车号。每次触发只发送一个 PDF，不发送合并转发消息或逐张图片。

## 文件结构

- `main.py`：插件入口和处理逻辑
- `metadata.yaml`：AstrBot 插件元数据
- `_conf_schema.json`：WebUI 插件配置
- `requirements.txt`：JMComic 依赖
- `tests/`：插件级测试

## 配置说明

常用配置包括：

- JMComic 网页端/API 客户端选择
- AVS Cookie 和网络代理
- 图片/章节下载并发数
- 群聊触发模式：白名单或黑名单（二选一）
- 群聊白名单和群聊黑名单（分别填写，多个群号用逗号、空格、分号或换行分隔）
- PDF 文件发送重试次数

下载图片和 PDF 只会写入 AstrBot 临时目录。PDF 发送完成后，插件会等待 QQ 协议端释放文件，再自动清理本次任务的临时文件；如果 Windows 报告文件仍被占用，会自动重试，不保存作品数据。

## 依赖来源

本插件使用 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 提供的 `jmcomic` Python API 和其内置 `Feature.export_pdf` 导出 PDF。`img2pdf` 依赖已写入 `requirements.txt`，AstrBot 加载插件时会按标准插件依赖流程自动安装；JMComic 官方文档中的异步下载接口为 `download_album_async()`。

本插件仅实现 QQ `aiocqhttp` 适配器。私聊默认允许触发；群聊白名单模式下仅允许白名单群触发，黑名单模式下允许除黑名单外的群触发。使用者应遵守相关平台、站点和当地法律法规的内容与访问规则。
