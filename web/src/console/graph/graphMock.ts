/**
 * 双图静态演示数据。
 *
 * 形状严格等于 `domain/session_graph.py::read_graph` 的返回：
 * `{revision, information_graph:{nodes,edges}, attack_graph:{nodes,edges}}`。
 * 接入真实数据时，只需把这里换成图查询接口的响应，视图层不需要改动。
 *
 * 全部为虚构内容：目标域名使用保留后缀 `.test`，UUID 与账号均为编造，
 * 不包含真实 CTF 目标、Flag、Cookie、Token 或任何凭据。
 *
 * 规模是按「会一直变大」准备的，不是刚好够看：
 * - 30 个实体 / 44 条关系，其中「用户管理接口 /api/users」有 9 条入边，
 *   用来验证单侧邻居过多时的折叠；
 * - 12 个攻击状态 / 18 次尝试（含 7 条自环）；
 * - 刻意保留不完美：2 个实体没有任何 `source_refs`，若干关系是
 *   hypothesis / refuted / superseded，若干尝试是 unknown / blocked / negative。
 */

import type { SessionGraphDocument } from "./graphSchema";

export const MOCK_SESSION_GRAPH: SessionGraphDocument = {
  revision: 12,
  information_graph: {
    nodes: [
      {
        id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        label: "目标 Web 应用 · portal.demo-target.test",
        source_refs: [
          "operation:a3842312-39c0-4189-b15c-7236889da34b",
          "artifact:7799ba98-6c16-49c9-a262-820cd60dbc4a"
        ]
      },
      {
        id: "43d573f8-7caf-46c0-9b8d-4e00d9a69cdd",
        label: "登录接口 /login",
        source_refs: [
          "operation:a3842312-39c0-4189-b15c-7236889da34b",
          "artifact:70ee4ebe-701c-45dd-a06a-669454202dbb"
        ]
      },
      {
        id: "45066611-268d-4be8-963d-47c15720809d",
        label: "会话 Cookie 策略",
        source_refs: [
          "operation:a3842312-39c0-4189-b15c-7236889da34b"
        ]
      },
      {
        id: "7b1e0524-f730-4ff8-81d0-5cc8ffe3f361",
        label: "测试账户 operator01",
        source_refs: [
          "operation:a3842312-39c0-4189-b15c-7236889da34b"
        ]
      },
      {
        id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        label: "用户管理接口 /api/users",
        source_refs: [
          "operation:755f459a-af5f-4e1c-b890-f5862e5ccbf0",
          "artifact:ee5dfb4a-bcb6-4be7-8eb4-c3dba428eeea"
        ]
      },
      {
        id: "bcbee52d-8f74-4547-a816-c200bf53ebd7",
        label: "用户详情接口 /api/users/{id}",
        source_refs: [
          "operation:e27f3dac-6c91-4d31-b168-f2c5faee9c16"
        ]
      },
      {
        id: "e12b651e-c799-4616-a1e8-aeceda396c26",
        label: "用户搜索接口 /api/users/search",
        source_refs: [
          "operation:b23c9349-4574-4a97-8833-73da1646372f",
          "artifact:edbbe059-a2b2-46b7-8d80-cab12b3a96d1"
        ]
      },
      {
        id: "f38dd34e-53b9-4041-be21-7b713dd42711",
        label: "批量导出接口 /api/users/export",
        source_refs: [
          "operation:e9dce30e-6c71-4188-a5b2-16c842ff9186"
        ]
      },
      {
        id: "91712797-0981-43ca-9ce9-46a3c572324c",
        label: "管理后台 /admin",
        source_refs: [
          "operation:e09f3cb4-3b2c-4dfc-a8b6-f2abdfb35f1f"
        ]
      },
      {
        id: "fbc7854d-59a5-484c-a38c-a7a060931fdf",
        label: "管理后台登录页 /admin/login",
        source_refs: [
          "operation:e09f3cb4-3b2c-4dfc-a8b6-f2abdfb35f1f"
        ]
      },
      {
        id: "c110a73d-110e-48ea-ba37-ea94aaec4323",
        label: "管理后台审计日志 /admin/audit",
        source_refs: [
          "operation:5cd409a2-4154-497f-a252-c0de80328ec1",
          "artifact:c5b3d1b0-8989-43ed-b37a-9d9e5e9aa7c7"
        ]
      },
      {
        id: "d4823d4d-a07c-49d4-b3e3-fb42e8417812",
        label: "文件上传接口 /api/upload",
        source_refs: [
          "operation:ea4e3086-95cd-4365-b0a5-501aeb7f0575",
          "artifact:51b72199-feb1-4361-9fe6-e93917b9e949"
        ]
      },
      {
        id: "cb201e6c-0308-49d0-9a61-08652ed202ae",
        label: "图片处理服务 /api/thumbnail",
        source_refs: [
          "operation:ea4e3086-95cd-4365-b0a5-501aeb7f0575"
        ]
      },
      {
        id: "9987e570-de03-49e2-b220-a692321d9b7a",
        label: "密码重置接口 /api/password/reset",
        source_refs: [
          "operation:c5fd1f93-1ac2-4de5-b423-980de69683b0"
        ]
      },
      {
        id: "ec490f8e-1654-4e98-9131-ecf89f2ff27f",
        label: "单点登录回调 /sso/callback",
        source_refs: [
          "operation:a38a1253-5007-4e33-95aa-67323e90995e"
        ]
      },
      {
        id: "6f0689dc-89af-4113-a396-0189543a39a4",
        label: "JWT 校验中间件",
        source_refs: [
          "operation:a38a1253-5007-4e33-95aa-67323e90995e",
          "artifact:6f521712-78d6-466e-b218-e5e4426a8a1d"
        ]
      },
      {
        id: "10f0e04b-b07f-422b-b417-822aa54fd608",
        label: "API 网关 nginx 1.24",
        source_refs: [
          "operation:656e4a22-7091-4707-b204-533c47b129fe"
        ]
      },
      {
        id: "3067fbde-4ac9-4756-b647-258ce2d3d6e9",
        label: "前端打包产物 main.4f2c.js",
        source_refs: [
          "operation:818789a8-85ed-404c-b2e2-e1f533b9eaa8",
          "artifact:43edeca9-6f52-401b-9601-f38dc6dc354e"
        ]
      },
      {
        id: "8cf476cf-48ba-4c9e-a6a6-62c924311704",
        label: "静态资源目录 /assets/",
        source_refs: [
          "operation:818789a8-85ed-404c-b2e2-e1f533b9eaa8"
        ]
      },
      {
        id: "9bc42383-1ecc-4197-8995-c67cd6d7e6b8",
        label: "对象存储桶 uploads-prod",
        source_refs: [
          "operation:ea4e3086-95cd-4365-b0a5-501aeb7f0575",
          "artifact:51b72199-feb1-4361-9fe6-e93917b9e949"
        ]
      },
      {
        id: "01a29f67-ab92-46d4-809c-d9fb5896e444",
        label: "错误页中的数据库线索",
        source_refs: [
          "operation:2fba9708-19cc-4501-98eb-f5151f38c9ec",
          "artifact:71dde199-f4ac-4e81-b3e2-8427a3908f12"
        ]
      },
      {
        id: "8b039bfe-e806-4fdf-b8a0-f14c7f6aa108",
        label: "数据库 PostgreSQL 15.4",
        source_refs: [
          "operation:2fba9708-19cc-4501-98eb-f5151f38c9ec",
          "artifact:71dde199-f4ac-4e81-b3e2-8427a3908f12"
        ]
      },
      {
        id: "a0a8aaca-a559-44bc-beb5-cd06a1e30f06",
        label: "数据库连接池配置",
        source_refs: []
      },
      {
        id: "fab3bf4e-09cf-4d50-8f47-b42e9505e7a5",
        label: "角色与权限模型",
        source_refs: []
      },
      {
        id: "517ae350-e474-4a4b-ad94-19c112f108bf",
        label: "定时任务调度器",
        source_refs: [
          "operation:a736fd70-36c2-4c7b-9a11-1b0e4ec3aeaf"
        ]
      },
      {
        id: "b788b067-d694-491e-b45f-77a1c6c3a16c",
        label: "邮件通知服务",
        source_refs: [
          "operation:c5fd1f93-1ac2-4de5-b423-980de69683b0"
        ]
      },
      {
        id: "15d51262-e3e8-4608-bfdb-a7804fc34906",
        label: "短信网关",
        source_refs: [
          "operation:c5fd1f93-1ac2-4de5-b423-980de69683b0"
        ]
      },
      {
        id: "45143d5c-9de0-4d38-b283-8f34db52e2d3",
        label: "第三方 OAuth 提供方",
        source_refs: [
          "operation:a38a1253-5007-4e33-95aa-67323e90995e"
        ]
      },
      {
        id: "34de8c4c-1337-41c4-81af-c339765cc192",
        label: "WAF CloudFence",
        source_refs: [
          "operation:6af978fd-4ab7-4759-bca5-6cebeca50cec"
        ]
      },
      {
        id: "3c916e83-7127-4d6e-953f-54ec01d0e99a",
        label: "备份文件 /backup/portal-2026-09.sql.gz",
        source_refs: [
          "operation:8a7f3482-22b2-4c70-a10c-4382f475734d",
          "artifact:02a4f9a0-2b6b-4121-8b9d-7d8b8203784e"
        ]
      }
    ],
    edges: [
      {
        id: "2bed7766-305b-420b-a13c-a0816a148087",
        from_id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        to_id: "43d573f8-7caf-46c0-9b8d-4e00d9a69cdd",
        relation: "对外暴露登录入口",
        status: "observed",
        source_refs: [
          "operation:a3842312-39c0-4189-b15c-7236889da34b"
        ]
      },
      {
        id: "49e0bd74-b1fa-4b67-9f9a-bf4c2ac4d58c",
        from_id: "7b1e0524-f730-4ff8-81d0-5cc8ffe3f361",
        to_id: "43d573f8-7caf-46c0-9b8d-4e00d9a69cdd",
        relation: "已授权的测试账户，用于登录",
        status: "observed",
        source_refs: [
          "operation:a3842312-39c0-4189-b15c-7236889da34b"
        ]
      },
      {
        id: "a0f09887-7899-4090-85c0-2fb5caa6c8c8",
        from_id: "43d573f8-7caf-46c0-9b8d-4e00d9a69cdd",
        to_id: "45066611-268d-4be8-963d-47c15720809d",
        relation: "登录成功后签发会话凭据",
        status: "observed",
        source_refs: [
          "operation:a3842312-39c0-4189-b15c-7236889da34b",
          "artifact:70ee4ebe-701c-45dd-a06a-669454202dbb"
        ]
      },
      {
        id: "454e1e80-51cf-4bb1-b31c-663c0dcf0c98",
        from_id: "45066611-268d-4be8-963d-47c15720809d",
        to_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        relation: "会话凭据可直接访问接口",
        status: "hypothesis",
        source_refs: [
          "operation:755f459a-af5f-4e1c-b890-f5862e5ccbf0"
        ]
      },
      {
        id: "a052ba2e-ff19-4af8-b82f-9d3d0e68cd8f",
        from_id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        to_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        relation: "对外提供用户管理接口",
        status: "observed",
        source_refs: [
          "operation:755f459a-af5f-4e1c-b890-f5862e5ccbf0"
        ]
      },
      {
        id: "002b4f20-8c4e-4c99-a0bb-0ebafda65ad4",
        from_id: "10f0e04b-b07f-422b-b417-822aa54fd608",
        to_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        relation: "网关路由到后端服务",
        status: "observed",
        source_refs: [
          "operation:656e4a22-7091-4707-b204-533c47b129fe"
        ]
      },
      {
        id: "0eb0b04f-d906-4022-8ffd-7efb2dbaa3ca",
        from_id: "6f0689dc-89af-4113-a396-0189543a39a4",
        to_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        relation: "校验 Bearer 令牌",
        status: "observed",
        source_refs: [
          "operation:a38a1253-5007-4e33-95aa-67323e90995e"
        ]
      },
      {
        id: "020fe4f3-0f1d-4773-bcb3-a156140eeba2",
        from_id: "3067fbde-4ac9-4756-b647-258ce2d3d6e9",
        to_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        relation: "前端页面调用该接口",
        status: "observed",
        source_refs: [
          "operation:818789a8-85ed-404c-b2e2-e1f533b9eaa8"
        ]
      },
      {
        id: "d6776753-b45f-43b9-852a-3b14a3ea8ccb",
        from_id: "517ae350-e474-4a4b-ad94-19c112f108bf",
        to_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        relation: "批量同步用户时调用",
        status: "observed",
        source_refs: [
          "operation:a736fd70-36c2-4c7b-9a11-1b0e4ec3aeaf"
        ]
      },
      {
        id: "4d90f1c7-6268-4626-afdc-c462af91f28f",
        from_id: "91712797-0981-43ca-9ce9-46a3c572324c",
        to_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        relation: "管理端复用用户接口",
        status: "hypothesis",
        source_refs: [
          "operation:e09f3cb4-3b2c-4dfc-a8b6-f2abdfb35f1f"
        ]
      },
      {
        id: "835f7569-36d0-4c6e-9875-79290a6e02a2",
        from_id: "c110a73d-110e-48ea-ba37-ea94aaec4323",
        to_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        relation: "记录接口调用",
        status: "observed",
        source_refs: [
          "operation:5cd409a2-4154-497f-a252-c0de80328ec1"
        ]
      },
      {
        id: "94594a0a-13fb-4afe-b381-aeb8ea639e6e",
        from_id: "3c916e83-7127-4d6e-953f-54ec01d0e99a",
        to_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        relation: "备份包含该表结构",
        status: "superseded",
        source_refs: [
          "operation:8a7f3482-22b2-4c70-a10c-4382f475734d"
        ]
      },
      {
        id: "34689d11-e951-4e38-9c26-1e971cc49e7a",
        from_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        to_id: "bcbee52d-8f74-4547-a816-c200bf53ebd7",
        relation: "路径参数指向子资源",
        status: "observed",
        source_refs: [
          "operation:e27f3dac-6c91-4d31-b168-f2c5faee9c16"
        ]
      },
      {
        id: "01f09494-dd2a-4a02-984f-5a94d8ad8b00",
        from_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        to_id: "e12b651e-c799-4616-a1e8-aeceda396c26",
        relation: "查询参数支持过滤",
        status: "observed",
        source_refs: [
          "operation:b23c9349-4574-4a97-8833-73da1646372f"
        ]
      },
      {
        id: "350d1a50-0ef5-450f-ab6b-5ff7d05f3e60",
        from_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        to_id: "f38dd34e-53b9-4041-be21-7b713dd42711",
        relation: "导出复用同一鉴权链路",
        status: "hypothesis",
        source_refs: [
          "operation:e9dce30e-6c71-4188-a5b2-16c842ff9186"
        ]
      },
      {
        id: "e5fe5ba3-4031-461c-993c-dcf6689370b5",
        from_id: "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
        to_id: "fab3bf4e-09cf-4d50-8f47-b42e9505e7a5",
        relation: "响应中区分角色字段",
        status: "hypothesis",
        source_refs: [
          "operation:755f459a-af5f-4e1c-b890-f5862e5ccbf0"
        ]
      },
      {
        id: "bf531804-9ec3-47d5-bcad-7b23b41b9693",
        from_id: "bcbee52d-8f74-4547-a816-c200bf53ebd7",
        to_id: "fab3bf4e-09cf-4d50-8f47-b42e9505e7a5",
        relation: "返回角色与部门字段",
        status: "observed",
        source_refs: [
          "operation:e27f3dac-6c91-4d31-b168-f2c5faee9c16"
        ]
      },
      {
        id: "239f525b-1003-4bfe-b338-6ca806a37b8a",
        from_id: "e12b651e-c799-4616-a1e8-aeceda396c26",
        to_id: "fab3bf4e-09cf-4d50-8f47-b42e9505e7a5",
        relation: "搜索结果携带角色",
        status: "observed",
        source_refs: [
          "operation:b23c9349-4574-4a97-8833-73da1646372f"
        ]
      },
      {
        id: "705a0146-0d75-40ef-a100-3074e91808dd",
        from_id: "f38dd34e-53b9-4041-be21-7b713dd42711",
        to_id: "91712797-0981-43ca-9ce9-46a3c572324c",
        relation: "导出文件包含管理端字段",
        status: "hypothesis",
        source_refs: [
          "operation:e9dce30e-6c71-4188-a5b2-16c842ff9186"
        ]
      },
      {
        id: "6446b7ea-91b8-465d-a465-a20e54945ce6",
        from_id: "91712797-0981-43ca-9ce9-46a3c572324c",
        to_id: "c110a73d-110e-48ea-ba37-ea94aaec4323",
        relation: "管理端写入审计日志",
        status: "observed",
        source_refs: [
          "operation:5cd409a2-4154-497f-a252-c0de80328ec1"
        ]
      },
      {
        id: "c2f1c3c7-8dcd-424c-970d-170be8c78f33",
        from_id: "fbc7854d-59a5-484c-a38c-a7a060931fdf",
        to_id: "91712797-0981-43ca-9ce9-46a3c572324c",
        relation: "登录后进入管理后台",
        status: "observed",
        source_refs: [
          "operation:e09f3cb4-3b2c-4dfc-a8b6-f2abdfb35f1f"
        ]
      },
      {
        id: "8f6c70c9-ebaa-4fba-8472-805caa4914a3",
        from_id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        to_id: "91712797-0981-43ca-9ce9-46a3c572324c",
        relation: "存在独立管理入口",
        status: "observed",
        source_refs: [
          "operation:e09f3cb4-3b2c-4dfc-a8b6-f2abdfb35f1f"
        ]
      },
      {
        id: "255e4eb2-e5a8-4376-bcfc-c24cdef407ab",
        from_id: "91712797-0981-43ca-9ce9-46a3c572324c",
        to_id: "45066611-268d-4be8-963d-47c15720809d",
        relation: "管理端使用同一会话策略",
        status: "hypothesis",
        source_refs: [
          "operation:e09f3cb4-3b2c-4dfc-a8b6-f2abdfb35f1f"
        ]
      },
      {
        id: "92db9098-dbd2-4717-aa42-570419bb60f0",
        from_id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        to_id: "d4823d4d-a07c-49d4-b3e3-fb42e8417812",
        relation: "对外暴露文件上传接口",
        status: "observed",
        source_refs: [
          "operation:ea4e3086-95cd-4365-b0a5-501aeb7f0575"
        ]
      },
      {
        id: "fae2b380-43d8-4e66-8163-50a71a2814b2",
        from_id: "d4823d4d-a07c-49d4-b3e3-fb42e8417812",
        to_id: "9bc42383-1ecc-4197-8995-c67cd6d7e6b8",
        relation: "文件写入对象存储",
        status: "observed",
        source_refs: [
          "operation:ea4e3086-95cd-4365-b0a5-501aeb7f0575",
          "artifact:51b72199-feb1-4361-9fe6-e93917b9e949"
        ]
      },
      {
        id: "8c395601-1af9-4e8e-9216-ab774da28f2d",
        from_id: "d4823d4d-a07c-49d4-b3e3-fb42e8417812",
        to_id: "cb201e6c-0308-49d0-9a61-08652ed202ae",
        relation: "上传后可请求缩略图",
        status: "observed",
        source_refs: [
          "operation:ea4e3086-95cd-4365-b0a5-501aeb7f0575"
        ]
      },
      {
        id: "bd677109-a769-4022-b185-d7d491151597",
        from_id: "9bc42383-1ecc-4197-8995-c67cd6d7e6b8",
        to_id: "8cf476cf-48ba-4c9e-a6a6-62c924311704",
        relation: "存储桶映射到静态目录",
        status: "refuted",
        source_refs: [
          "operation:ea4e3086-95cd-4365-b0a5-501aeb7f0575",
          "artifact:51b72199-feb1-4361-9fe6-e93917b9e949"
        ]
      },
      {
        id: "e2833c5f-3970-40f9-a194-96c02b7f0a23",
        from_id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        to_id: "9987e570-de03-49e2-b220-a692321d9b7a",
        relation: "暴露密码重置入口",
        status: "observed",
        source_refs: [
          "operation:c5fd1f93-1ac2-4de5-b423-980de69683b0"
        ]
      },
      {
        id: "e35b00da-70bc-4cc6-b5a5-c24fcefaaba3",
        from_id: "9987e570-de03-49e2-b220-a692321d9b7a",
        to_id: "b788b067-d694-491e-b45f-77a1c6c3a16c",
        relation: "通过邮件服务下发重置链接",
        status: "observed",
        source_refs: [
          "operation:c5fd1f93-1ac2-4de5-b423-980de69683b0"
        ]
      },
      {
        id: "0559d904-e0b7-4a4b-bd74-f7028f0a6487",
        from_id: "9987e570-de03-49e2-b220-a692321d9b7a",
        to_id: "15d51262-e3e8-4608-bfdb-a7804fc34906",
        relation: "通过短信网关下发验证码",
        status: "observed",
        source_refs: [
          "operation:c5fd1f93-1ac2-4de5-b423-980de69683b0"
        ]
      },
      {
        id: "8516a53f-c728-47ca-a9b7-22a2aa3c8fcb",
        from_id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        to_id: "ec490f8e-1654-4e98-9131-ecf89f2ff27f",
        relation: "存在单点登录回调",
        status: "observed",
        source_refs: [
          "operation:a38a1253-5007-4e33-95aa-67323e90995e"
        ]
      },
      {
        id: "41d5405f-7e59-46bd-bb43-c73c7246d0cc",
        from_id: "ec490f8e-1654-4e98-9131-ecf89f2ff27f",
        to_id: "45143d5c-9de0-4d38-b283-8f34db52e2d3",
        relation: "依赖第三方 OAuth 提供方",
        status: "observed",
        source_refs: [
          "operation:a38a1253-5007-4e33-95aa-67323e90995e"
        ]
      },
      {
        id: "46c319c8-2781-4903-a4fb-168b8ba3bea8",
        from_id: "ec490f8e-1654-4e98-9131-ecf89f2ff27f",
        to_id: "6f0689dc-89af-4113-a396-0189543a39a4",
        relation: "回调后签发 JWT",
        status: "observed",
        source_refs: [
          "operation:a38a1253-5007-4e33-95aa-67323e90995e",
          "artifact:6f521712-78d6-466e-b218-e5e4426a8a1d"
        ]
      },
      {
        id: "b7cd4b26-97f4-4612-9490-14db10819ab1",
        from_id: "6f0689dc-89af-4113-a396-0189543a39a4",
        to_id: "45066611-268d-4be8-963d-47c15720809d",
        relation: "校验失败回落到会话 Cookie",
        status: "hypothesis",
        source_refs: [
          "operation:a38a1253-5007-4e33-95aa-67323e90995e"
        ]
      },
      {
        id: "7b65626f-612b-4e1a-8a6e-83971b60d7fd",
        from_id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        to_id: "3067fbde-4ac9-4756-b647-258ce2d3d6e9",
        relation: "首页加载前端打包产物",
        status: "observed",
        source_refs: [
          "operation:818789a8-85ed-404c-b2e2-e1f533b9eaa8"
        ]
      },
      {
        id: "59892bab-dcb4-4e66-b6ef-3600d8d1bd08",
        from_id: "3067fbde-4ac9-4756-b647-258ce2d3d6e9",
        to_id: "8cf476cf-48ba-4c9e-a6a6-62c924311704",
        relation: "从静态目录加载资源",
        status: "observed",
        source_refs: [
          "operation:818789a8-85ed-404c-b2e2-e1f533b9eaa8"
        ]
      },
      {
        id: "815aad30-64f2-480e-9c42-4a1c6fa4373e",
        from_id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        to_id: "01a29f67-ab92-46d4-809c-d9fb5896e444",
        relation: "异常响应暴露数据库线索",
        status: "observed",
        source_refs: [
          "operation:2fba9708-19cc-4501-98eb-f5151f38c9ec",
          "artifact:71dde199-f4ac-4e81-b3e2-8427a3908f12"
        ]
      },
      {
        id: "fa1509c4-5109-414e-b162-13f8355c6e37",
        from_id: "01a29f67-ab92-46d4-809c-d9fb5896e444",
        to_id: "8b039bfe-e806-4fdf-b8a0-f14c7f6aa108",
        relation: "错误堆栈包含版本号",
        status: "observed",
        source_refs: [
          "operation:2fba9708-19cc-4501-98eb-f5151f38c9ec",
          "artifact:71dde199-f4ac-4e81-b3e2-8427a3908f12"
        ]
      },
      {
        id: "fe2b0b8a-8e5c-4640-9a97-9ed2dec1b01d",
        from_id: "8b039bfe-e806-4fdf-b8a0-f14c7f6aa108",
        to_id: "a0a8aaca-a559-44bc-beb5-cd06a1e30f06",
        relation: "连接池参数可从错误页推断",
        status: "hypothesis",
        source_refs: [
          "operation:2fba9708-19cc-4501-98eb-f5151f38c9ec"
        ]
      },
      {
        id: "df550a5b-9066-4f05-ae14-ed21f4d9b466",
        from_id: "01a29f67-ab92-46d4-809c-d9fb5896e444",
        to_id: "fab3bf4e-09cf-4d50-8f47-b42e9505e7a5",
        relation: "凭据表结构与角色字段对应",
        status: "superseded",
        source_refs: [
          "operation:2fba9708-19cc-4501-98eb-f5151f38c9ec"
        ]
      },
      {
        id: "a2864e37-9076-4016-bd5e-9225097e9615",
        from_id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        to_id: "34de8c4c-1337-41c4-81af-c339765cc192",
        relation: "前置 WAF 拦截注入类请求",
        status: "observed",
        source_refs: [
          "operation:6af978fd-4ab7-4759-bca5-6cebeca50cec"
        ]
      },
      {
        id: "6de44bd1-4498-44ee-a835-8f6e98a691c7",
        from_id: "34de8c4c-1337-41c4-81af-c339765cc192",
        to_id: "01a29f67-ab92-46d4-809c-d9fb5896e444",
        relation: "WAF 对错误页不做过滤",
        status: "refuted",
        source_refs: [
          "operation:6af978fd-4ab7-4759-bca5-6cebeca50cec"
        ]
      },
      {
        id: "2fce8bbb-2a24-452f-8e71-36b6c0023896",
        from_id: "38edf9a3-6e65-4f18-b971-0504ae75030c",
        to_id: "3c916e83-7127-4d6e-953f-54ec01d0e99a",
        relation: "备份目录可列举",
        status: "observed",
        source_refs: [
          "operation:8a7f3482-22b2-4c70-a10c-4382f475734d",
          "artifact:02a4f9a0-2b6b-4121-8b9d-7d8b8203784e"
        ]
      },
      {
        id: "a14b87a1-a50a-4c7f-875b-354d0f5e52ae",
        from_id: "3c916e83-7127-4d6e-953f-54ec01d0e99a",
        to_id: "8b039bfe-e806-4fdf-b8a0-f14c7f6aa108",
        relation: "备份包含完整数据库",
        status: "superseded",
        source_refs: [
          "operation:8a7f3482-22b2-4c70-a10c-4382f475734d"
        ]
      }
    ]
  },
  attack_graph: {
    nodes: [
      {
        id: "9368cb33-aa51-4a2c-908d-a348df3e0e61",
        summary: "认证入口已定位，访问控制尚未验证",
        entity_refs: [
          "43d573f8-7caf-46c0-9b8d-4e00d9a69cdd",
          "38edf9a3-6e65-4f18-b971-0504ae75030c"
        ]
      },
      {
        id: "c15541ea-73cc-41a2-a8d0-f6fcac596317",
        summary: "已持有测试账户会话，用户接口可达",
        entity_refs: [
          "7b1e0524-f730-4ff8-81d0-5cc8ffe3f361",
          "45066611-268d-4be8-963d-47c15720809d"
        ]
      },
      {
        id: "dd1781f9-1d8b-4b11-8fa8-2d0f8f9299ad",
        summary: "用户接口返回他人资源，是否越权仍无法确认",
        entity_refs: [
          "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85",
          "45066611-268d-4be8-963d-47c15720809d"
        ]
      },
      {
        id: "9f9fe144-5d33-4b10-8e04-e3a9bdfe20d7",
        summary: "管理后台拒绝当前会话，鉴权边界未变化",
        entity_refs: [
          "91712797-0981-43ca-9ce9-46a3c572324c"
        ]
      },
      {
        id: "fe23c44e-3591-4fe5-8dfb-53c68c2d0340",
        summary: "异常响应暴露数据库版本线索",
        entity_refs: [
          "01a29f67-ab92-46d4-809c-d9fb5896e444"
        ]
      },
      {
        id: "b82102da-9e43-428e-a540-674d1a9dc772",
        summary: "上传路径与管理端不同源，该线索已否定",
        entity_refs: [
          "d4823d4d-a07c-49d4-b3e3-fb42e8417812",
          "91712797-0981-43ca-9ce9-46a3c572324c"
        ]
      },
      {
        id: "6c65d87f-e1f0-440d-8d31-8224141e6f00",
        summary: "批量导出接口未做对象级校验，待独立复核",
        entity_refs: [
          "f38dd34e-53b9-4041-be21-7b713dd42711",
          "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85"
        ]
      },
      {
        id: "963e7cb8-4c15-4c39-a7de-f0cb756d74cf",
        summary: "搜索接口可枚举全部用户，低权限亦可见",
        entity_refs: [
          "e12b651e-c799-4616-a1e8-aeceda396c26",
          "c95d0f26-cd5f-4356-86d8-7a5b3c2d0e85"
        ]
      },
      {
        id: "8e568e38-22e4-4d5c-8bbe-92377e1e48cb",
        summary: "密码重置令牌存在可预测迹象",
        entity_refs: [
          "9987e570-de03-49e2-b220-a692321d9b7a"
        ]
      },
      {
        id: "eea947eb-b37e-4f86-b089-452086167052",
        summary: "管理后台审计日志可被普通会话读取",
        entity_refs: [
          "c110a73d-110e-48ea-ba37-ea94aaec4323",
          "91712797-0981-43ca-9ce9-46a3c572324c"
        ]
      },
      {
        id: "461654b3-d829-4edf-88ca-75f9bccc090c",
        summary: "WAF 对注入类请求返回 403，注入路径受阻",
        entity_refs: [
          "34de8c4c-1337-41c4-81af-c339765cc192",
          "e12b651e-c799-4616-a1e8-aeceda396c26"
        ]
      },
      {
        id: "b50f5e0a-2de5-453e-a11f-e914a3da5a12",
        summary: "备份文件可下载，但内容尚未核对",
        entity_refs: [
          "3c916e83-7127-4d6e-953f-54ec01d0e99a",
          "8b039bfe-e806-4fdf-b8a0-f14c7f6aa108"
        ]
      }
    ],
    edges: [
      {
        id: "76302cac-b27e-4b66-bc32-61612d7ab0fc",
        from_id: "9368cb33-aa51-4a2c-908d-a348df3e0e61",
        to_id: "c15541ea-73cc-41a2-a8d0-f6fcac596317",
        action: "使用测试账户 operator01 登录并保留会话",
        outcome: "success",
        source_refs: [
          "operation:a3842312-39c0-4189-b15c-7236889da34b",
          "artifact:70ee4ebe-701c-45dd-a06a-669454202dbb"
        ]
      },
      {
        id: "a0738270-6608-4029-b18e-85fd38dcdc40",
        from_id: "9368cb33-aa51-4a2c-908d-a348df3e0e61",
        to_id: "9368cb33-aa51-4a2c-908d-a348df3e0e61",
        action: "匿名请求 /api/users，不带会话",
        outcome: "blocked",
        source_refs: [
          "operation:48932552-75ce-4e06-9957-c4fff8720a9a"
        ]
      },
      {
        id: "b0b69b54-5023-43bf-aa9e-e27c20caa57e",
        from_id: "c15541ea-73cc-41a2-a8d0-f6fcac596317",
        to_id: "dd1781f9-1d8b-4b11-8fa8-2d0f8f9299ad",
        action: "以账户 A 的会话请求账户 B 的用户资源",
        outcome: "unknown",
        source_refs: [
          "operation:755f459a-af5f-4e1c-b890-f5862e5ccbf0",
          "artifact:ee5dfb4a-bcb6-4be7-8eb4-c3dba428eeea"
        ]
      },
      {
        id: "1df70ec2-d453-4d0b-897a-2b783b56d50c",
        from_id: "c15541ea-73cc-41a2-a8d0-f6fcac596317",
        to_id: "fe23c44e-3591-4fe5-8dfb-53c68c2d0340",
        action: "请求不存在的资源 ID，触发错误页",
        outcome: "success",
        source_refs: [
          "operation:2fba9708-19cc-4501-98eb-f5151f38c9ec",
          "artifact:71dde199-f4ac-4e81-b3e2-8427a3908f12"
        ]
      },
      {
        id: "504499bf-e52f-450b-bb44-695efeb1c396",
        from_id: "dd1781f9-1d8b-4b11-8fa8-2d0f8f9299ad",
        to_id: "9f9fe144-5d33-4b10-8e04-e3a9bdfe20d7",
        action: "使用同一会话访问 /admin",
        outcome: "blocked",
        source_refs: [
          "operation:e09f3cb4-3b2c-4dfc-a8b6-f2abdfb35f1f"
        ]
      },
      {
        id: "f66ff7e8-faff-43f0-91e8-c4489340742e",
        from_id: "9f9fe144-5d33-4b10-8e04-e3a9bdfe20d7",
        to_id: "9f9fe144-5d33-4b10-8e04-e3a9bdfe20d7",
        action: "枚举管理后台常见路由前缀",
        outcome: "negative",
        source_refs: [
          "operation:e09f3cb4-3b2c-4dfc-a8b6-f2abdfb35f1f",
          "artifact:43edeca9-6f52-401b-9601-f38dc6dc354e"
        ]
      },
      {
        id: "db01af3c-676d-4fb9-b25d-31de3f3288a2",
        from_id: "9368cb33-aa51-4a2c-908d-a348df3e0e61",
        to_id: "b82102da-9e43-428e-a540-674d1a9dc772",
        action: "上传测试文件并尝试从管理端路径访问",
        outcome: "negative",
        source_refs: [
          "operation:ea4e3086-95cd-4365-b0a5-501aeb7f0575",
          "artifact:51b72199-feb1-4361-9fe6-e93917b9e949"
        ]
      },
      {
        id: "a18a2d9a-e35a-4602-9d4a-e777a2e5b205",
        from_id: "b82102da-9e43-428e-a540-674d1a9dc772",
        to_id: "b82102da-9e43-428e-a540-674d1a9dc772",
        action: "更换扩展名重试上传",
        outcome: "blocked",
        source_refs: [
          "operation:ea4e3086-95cd-4365-b0a5-501aeb7f0575"
        ]
      },
      {
        id: "798de968-7104-4683-902a-be56af741a9c",
        from_id: "dd1781f9-1d8b-4b11-8fa8-2d0f8f9299ad",
        to_id: "6c65d87f-e1f0-440d-8d31-8224141e6f00",
        action: "以低权限账户调用批量导出",
        outcome: "unknown",
        source_refs: [
          "operation:e9dce30e-6c71-4188-a5b2-16c842ff9186"
        ]
      },
      {
        id: "89317e2e-4bd8-4487-b84d-0b492e07acc6",
        from_id: "6c65d87f-e1f0-440d-8d31-8224141e6f00",
        to_id: "963e7cb8-4c15-4c39-a7de-f0cb756d74cf",
        action: "用搜索接口遍历用户列表",
        outcome: "success",
        source_refs: [
          "operation:b23c9349-4574-4a97-8833-73da1646372f",
          "artifact:edbbe059-a2b2-46b7-8d80-cab12b3a96d1"
        ]
      },
      {
        id: "a5c272ba-4adb-4129-b16e-ae51eba373e8",
        from_id: "963e7cb8-4c15-4c39-a7de-f0cb756d74cf",
        to_id: "963e7cb8-4c15-4c39-a7de-f0cb756d74cf",
        action: "在排序参数中注入片段",
        outcome: "blocked",
        source_refs: [
          "operation:b23c9349-4574-4a97-8833-73da1646372f"
        ]
      },
      {
        id: "734ced7c-7de6-4dc2-aae9-9c6c8d3b1feb",
        from_id: "963e7cb8-4c15-4c39-a7de-f0cb756d74cf",
        to_id: "8e568e38-22e4-4d5c-8bbe-92377e1e48cb",
        action: "触发密码重置并采集令牌样本",
        outcome: "success",
        source_refs: [
          "operation:c5fd1f93-1ac2-4de5-b423-980de69683b0"
        ]
      },
      {
        id: "7fe81472-5018-444d-a6a0-d01373ab79f6",
        from_id: "8e568e38-22e4-4d5c-8bbe-92377e1e48cb",
        to_id: "8e568e38-22e4-4d5c-8bbe-92377e1e48cb",
        action: "用时间偏移预测下一个令牌",
        outcome: "unknown",
        source_refs: [
          "operation:c5fd1f93-1ac2-4de5-b423-980de69683b0"
        ]
      },
      {
        id: "da475a61-c1db-4ad1-ad97-4a823598cfeb",
        from_id: "c15541ea-73cc-41a2-a8d0-f6fcac596317",
        to_id: "eea947eb-b37e-4f86-b089-452086167052",
        action: "用普通会话读取 /admin/audit",
        outcome: "unknown",
        source_refs: [
          "operation:5cd409a2-4154-497f-a252-c0de80328ec1",
          "artifact:c5b3d1b0-8989-43ed-b37a-9d9e5e9aa7c7"
        ]
      },
      {
        id: "0090f264-13b0-4a1a-bd12-3bbe47561e67",
        from_id: "eea947eb-b37e-4f86-b089-452086167052",
        to_id: "461654b3-d829-4edf-88ca-75f9bccc090c",
        action: "在搜索参数中注入联合查询片段",
        outcome: "blocked",
        source_refs: [
          "operation:6af978fd-4ab7-4759-bca5-6cebeca50cec"
        ]
      },
      {
        id: "e18de3fd-7dbb-4123-abab-a3cc48b57044",
        from_id: "461654b3-d829-4edf-88ca-75f9bccc090c",
        to_id: "461654b3-d829-4edf-88ca-75f9bccc090c",
        action: "更换编码后重试同一注入",
        outcome: "blocked",
        source_refs: [
          "operation:6af978fd-4ab7-4759-bca5-6cebeca50cec"
        ]
      },
      {
        id: "b220e4e3-fb30-4c27-b536-c3df051fdc6d",
        from_id: "9368cb33-aa51-4a2c-908d-a348df3e0e61",
        to_id: "b50f5e0a-2de5-453e-a11f-e914a3da5a12",
        action: "列举 /backup/ 目录",
        outcome: "success",
        source_refs: [
          "operation:8a7f3482-22b2-4c70-a10c-4382f475734d",
          "artifact:02a4f9a0-2b6b-4121-8b9d-7d8b8203784e"
        ]
      },
      {
        id: "ce0a40d3-d1d0-4d76-9aa5-d6fc9b0e68bd",
        from_id: "b50f5e0a-2de5-453e-a11f-e914a3da5a12",
        to_id: "b50f5e0a-2de5-453e-a11f-e914a3da5a12",
        action: "下载备份文件并核对完整性",
        outcome: "unknown",
        source_refs: [
          "operation:8a7f3482-22b2-4c70-a10c-4382f475734d"
        ]
      }
    ]
  }
};
