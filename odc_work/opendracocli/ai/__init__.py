"""OpenDracoCLI P3 AI 智能层

模块:
  - client.py: LLM 客户端（OpenAI 兼容，复用 DracoHub Adapter 降级策略）
  - character.py: 命令角色卡（复用 character_card.json schema 子集）
  - emotion.py: 运维情感引擎（复用 EmotionEngine 4 因素混合 + 惯性）
  - prompt.py: system prompt 拼装（复用 MessageBuilder 顺序）
  - corrector.py: AI 纠错器
  - perception.py: 感知与自互动（上下文记忆）
  - risk_advisor.py: AI 增强风控（复用 SmartApproval 抗注入）
"""
