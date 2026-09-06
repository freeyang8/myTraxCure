# myTraxCure

用眼睛代替鼠标：注视即可选中段落，翻译结果直接覆盖在原文上。

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/freeyang8/myTraxCure?style=social)](https://github.com/freeyang8/myTraxCure)

---

## 项目简介

myTraxCure 是一款基于眼动追踪的桌面翻译软件：它通过摄像头实时捕捉你的注视点，自动锁定你正在阅读的段落，并将译文以覆盖层的形式叠加在原文上方，核心链路为 **眼动 → 选区 → 翻译 → 覆盖**。项目封装了 [eyetrax](https://pypi.org/project/eyetrax/) 0.4.0 作为眼动核心，配合可插拔的本地大模型翻译引擎（当前为 Ollama），全程数据不出本机。适合需要长时间阅读外文 PDF、Word 或扫描件的学习者、研究者，以及手部操作不便的 accessibility 用户。

---

## 功能特性

- **眼动追踪与启动校准引导**：摄像头实时追踪注视点，内置三态决策的启动校准流程与校准结果持久化
- **凝视稳定判定与段落锁定**：段落级众数判定 + 矩形锁定 + 磁滞去抖，避免误触发
- **空格触发翻译 / Esc 取消**：键鼠操作极简，无需点击
- **多格式文档解析**：PDF（文本层）、TXT、Word、图片/扫描件（PaddleOCR），支持滚动与缩放
- **可插拔翻译引擎**：工厂模式切换后端，当前内置 Ollama 本地翻译，后期可接入内置模型
- **鼠标模式降级兜底**：眼动不可用时自动降级为鼠标模拟注视，保证可用性
- **SQLite 翻译缓存**：完整键缓存 + 隐私失效机制，重复段落秒级响应

---

## 技术栈

- **GUI**：PyQt6
- **眼动核心**：eyetrax 0.4.0（封装复用）
- **人脸/虹膜检测**：MediaPipe FaceLandmarker
- **注视回归模型**：scikit-learn（ridge / elastic_net / svr / tiny_mlp）
- **滤波**：OpenCV KalmanFilter + KDE 置信轮廓
- **文件解析**：PyMuPDF（PDF）/ python-docx（Word）/ PaddleOCR（图片 OCR，可选）
- **翻译**：Ollama 本地大模型（可插拔架构）
- **缓存**：SQLite

---

## 快速开始

### 环境要求

- Python >= 3.10
- 笔记本摄像头即可（用于眼动追踪）
- NumPy 必须 < 2（本项目锁定 1.26.4，与 eyetrax 兼容）
- 可选：Ollama（本地翻译后端）、PaddleOCR（图片/扫描件 OCR）

### 安装与运行

```bash
# 克隆项目
git clone https://github.com/freeyang8/myTraxCure.git

# 进入目录
cd myTraxCure

# 创建并激活虚拟环境
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux / macOS

# 安装依赖
pip install -r requirements.txt

# 启动应用
python main.py
# 或
python -m mytraxcure
```
### 使用说明
> 首次启动会开启9点校准，以及收集降噪数据，且启动时间较长；若摄像头不可用，程序会自动降级为鼠标模式。OCR 与内置模型后端为可选依赖，详见 `pyproject.toml` 中的 `[project.optional-dependencies]`。

#### 模型配置
mytraxcure/core/config.py中TranslationConfig的变量和本地模型一致 
    model: str
    ollama_url: str



#### 验证连接

```bash
# 1. 确认模型已就绪（名称应与配置中的 model 一致）
ollama list

# 2. 确认服务可用（应返回已下载模型列表）
curl http://127.0.0.1:11434/api/tags
```

#### 常见问题

- **连接失败 / 一直重试**：Ollama 服务未启动，运行 `ollama serve` 或打开 Ollama 应用
- **提示模型不存在**：`model` 字段与 `ollama list` 的名称不一致，注意带上 `:latest` 等标签
- **翻译很慢**：换更小的模型，或确认没有其他程序占用显存/内存
- **术语翻译不统一**：准备 CSV 术语表（`原文,译文` 每行一条），将路径填入 `term_table_path`

---

## 待实现功能

项目仍在开发中，以下功能尚未实现（对应代码中的 TODO 留白）：

- **设置界面**：配置表单构建、读取与保存（`mytraxcure/ui/settings_view.py`）
- **数据清理**：一键清空翻译缓存、校准数据与上传文件（`mytraxcure/ui/settings_view.py`）
- **内置翻译引擎**：基于 transformers 的本地翻译后端（`mytraxcure/core/translation/builtin_engine.py`）
- **缓存管理**：删除指定文档缓存、清理过期缓存、清理孤儿缓存（`mytraxcure/core/translation/cache_store.py`）

---

> 本项目持续更新中。如果对你有帮助，欢迎 Star 支持；发现问题或建议欢迎提交 [Issue](https://github.com/freeyang8/myTraxCure/issues)，也欢迎 Fork 参与共建！
