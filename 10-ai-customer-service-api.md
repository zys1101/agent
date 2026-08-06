# AI 智能客服接口文档（直连 DeepSeek API 版）

- **文档编号**：API-AI-CS-001
- **版本**：1.0
- **协议**：HTTPS + JSON UTF-8
- **认证**：无（官网公开接口，无需登录）
- **关联代码**：后端 `E:\Appdata\Web\backend-api\src\modules\ai-service\`（`ai-service.controller.ts` / `ai-service.service.ts`）；前端 `E:\Appdata\Web\frontend-client\src\views\AiChat.vue`、`src\api\aiService.ts`
- **本地 Agent 版**：见 [11-ai-customer-service-local.md](11-ai-customer-service-local.md)

## 1. 概述

官网悬浮客服提供「AI 客服」入口，点击后进入 `/ai-service` 聊天界面。该版本为「直接调用 API」：后端直连 DeepSeek Chat 模型（OpenAI 兼容协议），一次请求返回回复。

## 2. Base URL

```text
https://<你的腾讯云网站域名>/api/v1/ai-service
```

本地开发：`http://localhost:3000/api/v1/ai-service`

## 3. 聊天接口

### `POST /ai-service/chat`

**用途**：发送一条用户消息，返回 AI 客服回复。无状态、幂等。

**请求头**

```http
Content-Type: application/json
```

**请求体**

```json
{
  "message": "如何发布设计需求？",
  "sessionId": "ai_1784000000000_ab12cd",
  "history": [
    { "role": "user", "content": "你们平台是做什么的？" },
    { "role": "assistant", "content": "我们是松辰智能，主营机械设计外包服务。" }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `message` | string | 是 | 用户本次消息，最长 2000 字 |
| `sessionId` | string | 否 | 会话标识（前端生成后回传，最长 64 字符） |
| `history` | array | 否 | 最近对话上下文（正序），服务端最多取最近 10 条；每条含 `role`（`user`/`assistant`）与 `content`（最长 4000 字） |

**成功响应 `200`**

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "reply": "您好，发布设计需求非常简单：登录后点击右上角「发布需求」……",
    "sessionId": "ai_1784000000000_ab12cd",
    "suggestions": ["AI 智能报价怎么用？", "平台有哪些设计服务？"]
  }
}
```

## 4. 配置说明（后端 `.env`）

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API Key（`.env` 中已配置） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 聊天模型名 |

## 5. 注意事项

- 服务端对 AI 调用失败做了兜底：返回固定降级文案（含人工客服电话/邮箱），HTTP 仍为 200。
- 上下文长度：服务端只取最近 10 条历史；前端本地最多保留 50 条。
- 成本控制：`max_tokens=800`、`temperature=0.6`。
- 上线建议：网关或控制器增加频控（如按 IP/会话每分钟 N 次）。
