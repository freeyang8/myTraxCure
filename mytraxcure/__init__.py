"""myTraxCure 包：摄像头眼动驱动的文档阅读与翻译桌面应用（PyQt6 实验原型）。

按职责分层：core 放配置、数据模型、凝视判定、文档解析与翻译，ui 放界面。
这里只对外暴露版本号，其余符号一律按需从子包导入——避免 import 本包就
拉起 PyQt6 / eyetrax / mediapipe 等重依赖。
"""

__version__ = "0.1.0"
