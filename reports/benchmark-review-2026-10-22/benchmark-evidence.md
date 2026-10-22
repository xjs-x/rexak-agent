# 三次真实测试证据索引

审计连接：PostgreSQL 使用 default_transaction_read_only=on，首次提取为 repeatable read；Checkpoint 只调用 Saver.get_tuple/SELECT，未运行 setup。Executor SQLite 通过既有 SSH 认证读取，URI mode=ro、PRAGMA query_only=ON，只做SELECT；远端文件只读。未通过产品客户端submit/control/reconcile等写接口。私有中间分析文件位于系统临时目录，不进入压缩包。

业务快照提取时间：2026-10-22T16:24:03.717+08:00。三次均已completed，后续复核其终态、更新时间和计数不变；这不是跨PostgreSQL、SQLite和文件系统的单一原子快照。

R1/R2/R3 表示报告中的三个样本；M为持久化物理模型请求序号，T为模型声明的工具序号。NULL Operation显示NOT_PERSISTED，表示该工具未走独立Operation落库，不是没有ToolCall。ID与hash可在原数据核对，正文只摘录无害验证输出。

## E0 运行来源与提取边界

审计前后核对的200个源码/决策文件SHA-256全部不变；原有git状态条目无增删，仅增加本次reports目录。此次没有修改生产代码、配置或运行记录。

| 测试 | Run | Session | Thread | 最后checkpoint | checkpoint数量 | 最终Artifact |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | e2856917-9842-4685-a9fa-79ba45ad4b9a | 7f4dbf91-8785-4876-9792-ad6c15018678 | session-542d8d52d1e74213b034ffa02a37e5e9 | 1f1cdee4-d313-6d8a-807d-659d6d3942b3 | 127 | 6267b527-030a-582d-b101-dde4bd2c9861 |
| R2 | dadda09c-ce80-4553-8cb0-cf6676e11796 | 576b0353-c824-47a9-bfa6-e8452fef44dd | session-3b77733cbdc745a6a9bab1195d83c844 | 1f1cdeef-56bf-67f3-8067-99ca18fcd556 | 105 | 7a8a6eb0-fd33-5e9b-8847-46083c7e9d2a |
| R3 | cfc0e9c6-433e-4e17-b22e-df92a54b81b0 | 9992ec77-2d0b-48be-a611-ca8fab0607ec | session-51f7ecdfae304ebea4860db9430a9994 | 1f1cdef9-ca7a-6d4c-8086-d511c6d18fb6 | 136 | a03c95d4-3ef2-598b-bfdf-45837ddcda1c |

三次共同指纹：`loaded-minimal-loop:09ca90ff7aad07ecd99ed886ff3439185521f1d9`，prompt_version=12，system_prompt_sha256=`c383e090606b3ae98a54573e820066f984672c78ea12ad87dfb51a35ca60e95e`；当前只导入函数计算的指纹与此一致。运行记录 source_commit=`79ecfff2891e095328a5dc2cccecc741d17cac18`，dirty=true，source_tree_hash=`535b9fa77ba79567ddcc1659d0a88a2ffecbea3583ac179ebad7d3aa4acdfed1`。loaded指纹只涵盖最小循环入口与提示，并非全进程字节码证明；source_tree_hash是当时非原子文件读取快照。未按当前HEAD推定历史加载。

## E1–E3 核心结果与原始输出

| Artifact ID | 字节/SHA-256 | source_operation_id | 会话 | 原文件相对key |
| --- | --- | --- | --- | --- |
| da16b44e-0e31-4112-a254-528404cbe4ed | 8 / 0ab1913ca486f9b32fc532f42711cbeb70e6daa93da654ddc92ce2a731372b9d | 77d3f25c-c029-4361-b672-43647273c768 | R1 | 0a/0ab1913ca486f9b32fc532f42711cbeb70e6daa93da654ddc92ce2a731372b9d |
| f3adee6a-24e8-40a5-a7b5-1b46949dd23c | 13 / ecdde384eef88d3dc7d613be66a03c709a9d9d0750d5474b58325e442f360cb4 | 6f4c87a4-0199-47c8-9992-ebb24f16bcbb | R1 | ec/ecdde384eef88d3dc7d613be66a03c709a9d9d0750d5474b58325e442f360cb4 |
| adf09310-1911-4256-b40b-2cd5684c9a43 | 68 / 4566ec4d71f3c132de25c907bcd5adea123b3093611f0ff741414989d1ffc6da | 0ac82d3f-aa38-474c-b7f7-73a1d8014736 | R1 | 45/4566ec4d71f3c132de25c907bcd5adea123b3093611f0ff741414989d1ffc6da |
| ea76c8fa-2731-4ba8-9b30-923913a406f9 | 495 / 3e94c2e4607edf518a8877a7cb037a5c1a562fff336878e8d4f2e2d9d9e71e9e | e61d37c0-268c-49f6-8cf3-f2786542f713 | R1 | 3e/3e94c2e4607edf518a8877a7cb037a5c1a562fff336878e8d4f2e2d9d9e71e9e |
| d19e2068-9658-4d28-88eb-2f27b98db821 | 16 / 2d9a0d76a0bf7d29d924dab78330824c3e7ab557e49488178b36d058124d1c07 | 79c3895d-33bf-479e-9a00-470f3e9112a7 | R1 | 2d/2d9a0d76a0bf7d29d924dab78330824c3e7ab557e49488178b36d058124d1c07 |
| 39cfccba-37b1-405d-b5b8-079976239bdf | 173 / ce99962bb5c7dddebbab10447fd1ad8302006e282d6db0eec8b9b29aa3b5fec4 | f2e87853-5289-41dd-b569-ec5714d7bc72 | R2 | ce/ce99962bb5c7dddebbab10447fd1ad8302006e282d6db0eec8b9b29aa3b5fec4 |
| 2c3fa2e1-e10a-4a12-97d6-c4502f28d8de | 1217 / 4fc2c3155099d4b9f74c18cf6fce2b2fd8b809b415bbdeb16b5f42d790ff2a14 | f2e87853-5289-41dd-b569-ec5714d7bc72 | R2 | 4f/4fc2c3155099d4b9f74c18cf6fce2b2fd8b809b415bbdeb16b5f42d790ff2a14 |
| 8b52c566-0f88-559d-b923-b7792b7f7725 | 361 / e9208f910eac4d40613f75a91f50ec350636f99d8846c9a90225e1638f5801e2 | NULL | R2 | e9/e9208f910eac4d40613f75a91f50ec350636f99d8846c9a90225e1638f5801e2 |
| da2077eb-f3d6-5a62-a214-4d7d9e3438f6 | 236 / 8e94c1a2d9edfec179df8ffdddac2da79237d65411dc4ca69d33e55c53037d3d | NULL | R3 | 8e/8e94c1a2d9edfec179df8ffdddac2da79237d65411dc4ca69d33e55c53037d3d |
| 57614e6d-055d-58b7-b5e6-d52e427a07ba | 359 / 5c2fc5657df437ee76c0464ff0935016d0af539bb85e19f5a39bc30d71cc74c4 | NULL | R3 | 5c/5c2fc5657df437ee76c0464ff0935016d0af539bb85e19f5a39bc30d71cc74c4 |
| d0e1dbc9-af79-52f1-b16e-d0d6dbe410cd | 927 / dd4ad16aa1e8ac851c8a7687cd872faab45471c3994ebdda5299911843f889cd | NULL | R3 | dd/dd4ad16aa1e8ac851c8a7687cd872faab45471c3994ebdda5299911843f889cd |
| 238de3e5-3561-4898-93e3-6eec2e6efb67 | 1151 / 3aab3e8c08bb92d236c148b02c71f47ca664612d434e8a0b6130cc5f8e127c5b | 801bf2b4-c7fb-4ae3-b8bb-feaea75ef940 | R3 | 3a/3aab3e8c08bb92d236c148b02c71f47ca664612d434e8a0b6130cc5f8e127c5b |
| 50b58c50-5832-413a-aa15-ca50a58f723d | 495 / 232963c92f4b7715502665877262f88e9f9faa8fd0942eeb974ac501e64e6538 | 801bf2b4-c7fb-4ae3-b8bb-feaea75ef940 | R3 | 23/232963c92f4b7715502665877262f88e9f9faa8fd0942eeb974ac501e64e6538 |
| 6267b527-030a-582d-b101-dde4bd2c9861 | 4499 / 8d52a1c6a5b19e037248859417f901d51ab5c89956cf0da5833f36d44ca91041 | NULL | R1 | 8d/8d52a1c6a5b19e037248859417f901d51ab5c89956cf0da5833f36d44ca91041 |
| 7a8a6eb0-fd33-5e9b-8847-46083c7e9d2a | 4020 / 760ed4f7ae477a412ef33e2dde8378e6f0fcb2e3a2f51bd1587d242a9a7e66ea | NULL | R2 | 76/760ed4f7ae477a412ef33e2dde8378e6f0fcb2e3a2f51bd1587d242a9a7e66ea |
| a03c95d4-3ef2-598b-bfdf-45837ddcda1c | 4798 / cb0c7f68eb20456ec8359c537e40ca7486efcaf4928881d0adf4359a451c66f9 | NULL | R3 | cb/cb0c7f68eb20456ec8359c537e40ca7486efcaf4928881d0adf4359a451c66f9 |

文件根为 `C:/xjs/rexak-agent/data/runtime-precontract-20261021/artifacts/`；相对key与该根拼接即原始文件，原件不复制进报告包。

### E1 SSTI命令id响应

Artifact `adf09310-1911-4256-b40b-2cd5684c9a43`：

```text
Hello uid=33(www-data) gid=33(www-data) groups=33(www-data),0(root)
```

### E1 浏览器文本

Artifact `d19e2068-9658-4d28-88eb-2f27b98db821`：

```text
Hello XSS_MARKER
```

### E2 HTTP RCE正文

Artifact `39cfccba-37b1-405d-b5b8-079976239bdf`：

```text
uid=1(daemon) gid=1(daemon) groups=1(daemon)
daemon
Linux 689875ce0984 6.6.87.2-microsoft-standard-WSL2 #1 SMP PREEMPT_DYNAMIC Thu Jun  5 18:30:46 UTC 2025 x86_64 GNU/Linux
```

### E3 首次curl RCE原始stdout

Artifact `da2077eb-f3d6-5a62-a214-4d7d9e3438f6`：

```text
PAYLOAD_LEN=806
curl: (18) transfer closed with outstanding read data remaining
HTTP/1.1 200 OK
Date: Thu, 22 Oct 2026 08:05:04 GMT
Transfer-Encoding: chunked
Server: Jetty(9.2.11.v20150529)

uid=0(root) gid=0(root) groups=0(root)
```

E3 HTTP响应头原文摘录（其余头不复制）：

```text
x-rce-out: uid=0(root) gid=0(root) groups=0(root) | root | 20a45bca6572 |
content-type: text/html; charset=UTF-8
content-length: 495
server: Jetty(9.2.11.v20150529)
```

## E4 结束退回与真实来源链

### R1

| session_seq | event_id | 时间(+08:00) | 决定/原因数量 |
| --- | --- | --- | --- |
| 172 | 0f0621d1-4473-4737-87ef-24795e310dce | 2026-10-22T15:58:00.608+08:00 | complete / 0 |

checkpoint finish_gate_feedback：`无拒绝`。

### R2

| session_seq | event_id | 时间(+08:00) | 决定/原因数量 |
| --- | --- | --- | --- |
| 87 | cd6ae751-24da-4aa2-a6ce-2763a3484b03 | 2026-10-22T16:02:11.878+08:00 | continue / 1 |
| 126 | 1701c8c6-ff55-4078-8489-91d18599abe8 | 2026-10-22T16:02:42.851+08:00 | complete / 0 |

checkpoint finish_gate_feedback：`结束提议引用不可用或其他会话证据`。

### R3

| session_seq | event_id | 时间(+08:00) | 决定/原因数量 |
| --- | --- | --- | --- |
| 87 | c421a4be-910a-4f2f-93fe-135063d8e5ef | 2026-10-22T16:05:47.329+08:00 | continue / 1 |
| 170 | a03cb8cc-b43d-4097-b69e-982335ce9d91 | 2026-10-22T16:07:23.421+08:00 | complete / 0 |

checkpoint finish_gate_feedback：`结束提议引用不可用或其他会话证据`。

4个日志工件的session_id均正确、availability=available、本地hash和远端stdout一致，但source_operation_id=NULL；FinishGate判定条件见S06，不检查MIME、source_url或captured_by_identity。模型对“二进制/缺URL”的解释只作为其可见决策证据，不采纳为根因。

| 日志Artifact | 远端原件 | 共同SHA-256 |
| --- | --- | --- |
| 8b52c566-0f88-559d-b923-b7792b7f7725 | /home/kali/rexak-agent-data/jobs/583b9283-8f17-4caa-a55b-702cf68c6111/stdout.log | e9208f910eac4d40613f75a91f50ec350636f99d8846c9a90225e1638f5801e2 |
| da2077eb-f3d6-5a62-a214-4d7d9e3438f6 | /home/kali/rexak-agent-data/jobs/2042bbb1-da1c-4ac5-82a5-6af3f9c2d8f1/stdout.log | 8e94c1a2d9edfec179df8ffdddac2da79237d65411dc4ca69d33e55c53037d3d |
| 57614e6d-055d-58b7-b5e6-d52e427a07ba | /home/kali/rexak-agent-data/jobs/fe7715e9-6c33-4ce6-a4f4-4818c17faf98/stdout.log | 5c2fc5657df437ee76c0464ff0935016d0af539bb85e19f5a39bc30d71cc74c4 |
| d0e1dbc9-af79-52f1-b16e-d0d6dbe410cd | /home/kali/rexak-agent-data/jobs/c1d8d802-d213-4267-89ed-16216d486d4b/stdout.log | dd4ad16aa1e8ac851c8a7687cd872faab45471c3994ebdda5299911843f889cd |

## E5 失败和状态差异

R1/T23：validation_failed，execution_certainty=not_applied；浏览器Job `7337964a-86be-4307-bb09-0848bc0c1744` 是同Session的真实Job，Ledger=succeeded，但execution_resources中没有该ID；Operation `330e07b0-f585-4b76-886c-b3b8fe5c85a3` 仍running。

R3/T3：Operation `2d11f08f-f1b0-426a-b92a-61401630275b`、Job `4b0af2b4-ccbe-4091-8d0b-123d7b27e76d` 均failed，错误http_transport_error / incomplete chunked read；ToolMessage明确execution_certainty=uncertain、delivery_state=unknown，result_artifact_ids=[]，远端该Job无已保存response文件。本次不能判断其载荷是否执行。随后T5新建Operation，不存在T3之后、T5之前的状态核对工具调用。

## E6 领域记录与状态

### findings
| id | session_id | status | title |
| --- | --- | --- | --- |
| 234ddbde-2a42-47cc-b706-d47e34ffd9bf | 7f4dbf91-8785-4876-9792-ad6c15018678 | candidate | 根路径 name 参数存在反射型 XSS（响应未做 HTML 转义） |
| a857b99e-1fcd-431b-804c-814fb5679995 | 7f4dbf91-8785-4876-9792-ad6c15018678 | candidate | 根路径 name 参数存在 Jinja2 服务端模板注入（SSTI），可导致远程命令执行 |
| 0ba8df57-baaa-45c6-bfb5-45a5715626d6 | 576b0353-c824-47a9-bfa6-e8452fef44dd | candidate | Apache HTTPD 2.4.49 路径穿越（CVE-2021-41773）导致 CGI 远程命令执行 |
| 6ad7615f-28e1-44e2-be4e-7c7d21105336 | 9992ec77-2d0b-48be-a611-ca8fab0607ec | candidate | Struts2 Content-Type OGNL 注入导致未认证 RCE（S2-045 / CVE-2017-5638） |

### observations
| id | session_id | source_run_id | source_operation_id |
| --- | --- | --- | --- |
| 1bd5a04b-43a4-4238-923c-31b107f078eb | 7f4dbf91-8785-4876-9792-ad6c15018678 | e2856917-9842-4685-a9fa-79ba45ad4b9a | 29a05c2a-b03c-4be8-abf7-35ca7e595a02 |
| 44a623e6-0bbb-486f-97d0-cd6c54be2ccb | 576b0353-c824-47a9-bfa6-e8452fef44dd | dadda09c-ce80-4553-8cb0-cf6676e11796 | 739ab682-a142-49c6-b924-22c460584184 |
| 2c806102-c045-4787-8089-c3cdb6ddc9ac | 9992ec77-2d0b-48be-a611-ca8fab0607ec | cfc0e9c6-433e-4e17-b22e-df92a54b81b0 | e4b1ef4a-510a-454b-86ff-96841152fd7a |
| 31fab757-d1b7-4225-82eb-3a7b308b680a | 9992ec77-2d0b-48be-a611-ca8fab0607ec | cfc0e9c6-433e-4e17-b22e-df92a54b81b0 | 39824902-411c-4a3b-8ed2-79b103651258 |

### assets
| id | session_id | method | path |
| --- | --- | --- | --- |
| baaf9f43-c0cb-4544-a0a5-0cab9bc5f9e0 | 576b0353-c824-47a9-bfa6-e8452fef44dd | POST | /cgi-bin/.%2e/.%2e/.%2e/.%2e/bin/sh |
| aeb5d7c6-86b5-434e-a707-87d0610c9dc0 | 9992ec77-2d0b-48be-a611-ca8fab0607ec | GET | / |
| 20ec7017-362d-4fc0-8e81-4052665f952d | 9992ec77-2d0b-48be-a611-ca8fab0607ec | POST | /index.action |

三Session均无Lead、Coverage、report_versions；Verification按finding_id JOIN到本次4个Finding核对为0；commands按Session查询为0。未以“表不存在run_id”为零记录依据。Observation.source_operation_id指向记录操作本身；其工具返回中的evidence_source_operation_ids以及artifact.source_operation_id才关联原始测试来源，二者不能混同。

## E7 完整模型与工具索引

摘要核对：HTTP 25个按实际payload反向映射请求参数及默认值；浏览器2个按其payload；命令8个按execution_v1原始参数；脚本1个用checkpoint历史ToolCall.code还原Ledger中已脱敏的code。按各自协议重算36/36匹配远端request_digest。业务表与远端摘要直接相等25/36；命令/脚本9个及浏览器2个使用不同摘要包结构，直接不等属于协议口径差异，不能判为执行内容不一致。[S21–S24]

### R1 模型请求

| 序号 | ModelCall ID | LogicalCall ID | 提交→完成(+08:00) | 供应商input/output/cache/reasoning | Context Artifact |
| --- | --- | --- | --- | --- | --- |
| M1 | 32dc1051-2101-4af2-a792-10d933117115 | d549dce2-58f4-907e-db4a-81640430829a | 15:55:31.459 → 15:55:33.521 | 15545/92/15360/NOT_AVAILABLE | 425f907f-7b53-4d32-9c48-3d906b0ef54e |
| M2 | 8a1ba9f1-6c56-45b9-926e-4543ab576a3f | e1ce739f-3e9b-ed2d-af71-239d62774ca3 | 15:55:35.266 → 15:55:39.281 | 17154/404/16384/102 | bb68506a-fe9d-40c3-89d0-926f3a55a11b |
| M3 | 2b813d24-7ae5-41eb-8030-e428c645d1d1 | 639e90f1-25f6-6cba-0506-b3a7d1c13c63 | 15:55:40.819 → 15:55:48.023 | 21861/552/21504/236 | 23a46621-2347-4460-872b-2967a3a1660e |
| M4 | a7f27557-6ee8-4807-a306-714b1969ae79 | 520d5faa-7123-f742-295e-9d2da93798c6 | 15:55:50.003 → 15:55:58.463 | 25899/841/25600/516 | 852ddb78-afee-41d0-9ede-da1f9c56cb9b |
| M5 | d7f0e9aa-7520-494f-813c-890a8f344efd | 9f1cb00d-6c33-9177-e179-2cee7c23dfe5 | 15:56:00.432 → 15:56:25.206 | 30397/2265/29696/1898 | f6a79872-47db-4643-b837-112839fcf8d3 |
| M6 | a3c76e2f-6f98-4037-9ada-5a98c810bb98 | 71024e47-71e9-56b0-7d3f-8b8735713e2d | 15:56:26.911 → 15:56:37.205 | 35727/839/34816/605 | 25a280f9-3d8f-4408-8c91-794e91b49546 |
| M7 | 9df96c6f-167d-45cf-b97a-788428272409 | 3035abe8-ae6a-ae29-765b-a3b06dc02dfe | 15:56:57.592 → 15:57:01.569 | 38376/267/0/83 | d02af78b-c5eb-4f2d-9a35-019020378e9c |
| M8 | bd326aca-b485-4cda-9700-d1cc69850364 | b6be747c-178b-3858-ba08-1e41b67f6538 | 15:57:03.225 → 15:57:09.231 | 40212/420/39936/294 | 538af37f-31b2-4504-8a5d-b7398158a139 |
| M9 | 03d780d8-18e3-44b1-9fb1-575486a3801a | cc9d72ba-9608-42e3-6eb2-0ac4f28464c2 | 15:57:09.602 → 15:57:12.810 | 40841/162/39936/90 | 32f1fdfc-03f5-42bb-bff1-81f5a0da4792 |
| M10 | ca9504ec-c2d7-4bee-a880-c2e4e91b1145 | 38a58114-c45d-cd92-9b36-b701c0ed2fae | 15:57:14.376 → 15:57:25.879 | 41823/494/40960/119 | d2fd68b4-456c-4a5e-ab72-b94c2245a48c |
| M11 | 53488d19-5fca-4b5a-ab34-e55af57adf43 | b5ccea73-53f1-889d-b897-cfd3b36b262f | 15:57:26.183 → 15:57:39.678 | 42891/1421/41984/281 | 289310af-2fc6-427d-8bd5-d91fd3e0c945 |
| M12 | e3f51253-4f69-4d32-b943-a505dc83fd95 | db288b6e-aed6-c21e-96e6-e742c62ad2f5 | 15:57:40.010 → 15:58:00.200 | 45530/1587/45056/55 | e60359d1-f96b-49e7-90f9-ed0d794624e6 |

### R1 工具调用

| 序号 | 工具 | ToolCall ID | Operation ID | Job ID | ToolMessage结果 | Artifact引用 |
| --- | --- | --- | --- | --- | --- | --- |
| T1/M1 | http_request | call_eddfc756daa742e0aa1dc143 | 6f240e92-7a13-4061-9f8b-9ff948d09291 | d4401c78-5deb-4b14-b16b-be9e6fa328fc | succeeded | 2f0db249-14c6-4a6d-ba6c-1801ea27ae95, e7ea6c3a-66af-418f-a9ed-7c98eda20d9a |
| T2/M1 | get_execution_environment | call_c8c870ad93014751a9b1ab89 | NOT_PERSISTED | — | succeeded | — |
| T3/M2 | http_request | call_f0b98cb3fc7f492fb7a36c23 | 892d2688-e51e-4bcc-873c-47604ff9a548 | 28f8eeed-6c91-4fdc-9c04-c78660e97f87 | succeeded | bd5fcd53-7600-44f3-8557-e858cf67f25b, 37c4ed2b-7d0f-4399-9f6b-7e055fdc7e99 |
| T4/M2 | http_request | call_16ed5b35240b4c5aab7444f6 | 49c10ded-8d1b-4a9d-afb3-d5b7717f3d88 | 067849fb-a578-4841-9957-3fd06c0ea8d0 | succeeded | 04b343ea-d270-47e7-97a9-9ac77b471035, da285df1-8265-48c1-beac-c682d7659afc |
| T5/M2 | http_request | call_4539e749d5bd4c0aa6f091ea | 568eb8ac-2425-4d37-8ed6-9f8346e28975 | 6b93e6e7-c56c-4296-996e-320033377e7c | succeeded | 34624700-c6bf-4743-809d-4604d5852a4c, d0482376-4c48-441c-a78f-65887c78a24a |
| T6/M2 | http_request | call_c5ef10efffae42c481d0144f | d2a28b43-12ab-46bd-b732-4a4e5db0b4bd | 7fcf2f52-d9c9-420a-bdac-f2e43aae4ce5 | succeeded | fe351f47-8de0-4501-9a4b-adb5f64bcb83, 5dbe7d07-72b3-4304-a969-5fd8c4d6ebec |
| T7/M2 | http_request | call_d10c1cd75cfa4beab9124c9f | 5b42372f-2d7f-4bd9-a63f-209685f7ea6e | 67491671-216f-4735-889a-523aa1ad15a7 | succeeded | 6c7ea782-b3ed-4a55-a1e9-7287a64dd809, 546fb446-159c-4d01-a43a-97335aff3c1f |
| T8/M3 | run_command | call_20edceafe0134a8c87fca900 | 79ce3117-bdcc-4bc3-ba19-8f6889e54d89 | 06d6ba4b-d473-463d-a6b9-3ce380626c73 | succeeded | — |
| T9/M3 | http_request | call_4ead17b8ce9d4bbe909fcb86 | 77d3f25c-c029-4361-b672-43647273c768 | 38141990-3ab0-4626-8082-8316908a9e00 | succeeded | da16b44e-0e31-4112-a254-528404cbe4ed, f9f4fe0a-0007-475d-a592-6d1980b5e695 |
| T10/M3 | http_request | call_0f87d98026fa47bd84a379ab | 46c9c970-687a-433f-a3fa-4719c3f397a0 | 7dc152fe-60ce-4f04-b1f1-878642e311ea | succeeded | 31b6cbce-827b-4cb2-a653-564c7890d45b, 0b6318be-c1bf-4914-be61-5f4e1bd88a49 |
| T11/M3 | http_request | call_8351f797dce84e8faa650b8f | 6f4c87a4-0199-47c8-9992-ebb24f16bcbb | d8ffc846-ebae-42ae-9020-f8e4f49da3d4 | succeeded | f3adee6a-24e8-40a5-a7b5-1b46949dd23c, 99759f53-d30b-4213-bbb2-d74c50aa9e33 |
| T12/M4 | http_request | call_cd9330438de049a7a2d276a0 | 8c1ac77a-341d-44d4-845b-f097c0ea6241 | c1563b0c-239f-448b-bd1d-dae15547de10 | succeeded | 5acb6766-1f21-4aaa-a095-a75629954c76, bff6d38a-48dc-497e-8bc4-1a00d6b22bd7 |
| T13/M4 | http_request | call_96c72d0f360a4dcc8d9233fe | afa8b9e6-5954-4dfd-b1de-0da8f50c60c5 | 1470afdf-48da-462c-a68f-219a76b4316b | succeeded | a88edee1-b1ab-43ee-bdc4-65e40d917f57, 02360d6f-1a19-4db0-aa70-d14e3fa0643e |
| T14/M4 | http_request | call_f78735f89d65427782896af9 | c6b7388a-9f4a-4a33-a5b7-469aba3c32dc | 3dd175f9-fc5b-459b-af48-ccd874fcb637 | succeeded | afb72199-b609-4607-9ab6-4329f65e9160, 4829b433-6186-4de0-90eb-b1974e799424 |
| T15/M4 | http_request | call_6d2d2144edf740f490644e37 | 0f5485cc-01fe-4c07-9d5b-2daa20ce5dc7 | 2fdc38c1-a22e-4483-86d8-c4924fda4c02 | succeeded | ed87b2f7-0abc-43c3-8140-c4cdff7f8cbb, 09fac347-944e-4f5c-b6c1-9e28a8104b45 |
| T16/M5 | http_request | call_242af2c5ece14fe8975cb9fb | 0ac82d3f-aa38-474c-b7f7-73a1d8014736 | 700d011c-00b4-4886-9dff-b78b5908223d | succeeded | adf09310-1911-4256-b40b-2cd5684c9a43, af4fb041-2e00-4125-aec1-f6076cc7fbb2 |
| T17/M5 | http_request | call_77d790ab065748f4a6c7afdb | e5d675ad-2baa-4901-b766-c28eaf40a98e | 8b2a305f-1bcd-4866-af82-ea74036cb3e2 | succeeded | 6982230b-cafb-40b2-8f47-b236962b20c5, 77915f0d-180a-48f3-bc76-5b3f9906f6f6 |
| T18/M5 | http_request | call_e149006944574ed39e194852 | bbb4cda9-b92c-4086-ac9d-3fc8854b0562 | 1f7a075a-9f2b-4d26-b458-e4dbe21cc5b0 | succeeded | fcf64f04-4ce0-434d-877e-da9fe5d83849, dab5075d-7d30-432d-9092-dce248f93991 |
| T19/M6 | browser_navigate | call_b02a900d418441458d52bea7 | 330e07b0-f585-4b76-886c-b3b8fe5c85a3 | 7337964a-86be-4307-bb09-0848bc0c1744 | accepted | — |
| T20/M6 | http_request | call_4a1a105942734928b8a693ee | c40e99d5-f9c4-4078-9855-018c2a49beba | 6dec0541-0720-4234-b7b0-c57db072735c | succeeded | 42f4a4d3-301b-428c-8b81-a30a401dd586, 8ed01df2-cbba-42d6-a521-fa6a66cf3061 |
| T21/M7 | get_job | call_7bc15d3b726e48ea8ebd3701 | NOT_PERSISTED | 7337964a-86be-4307-bb09-0848bc0c1744 | succeeded | — |
| T22/M7 | http_request | call_b2d8da0c06644f5e81ad997a | e61d37c0-268c-49f6-8cf3-f2786542f713 | 4a0c222d-35a7-434d-b7a5-f765216a7b73 | succeeded | ea76c8fa-2731-4ba8-9b30-923913a406f9, 3cda95a6-e2ee-4305-acc2-fd83e4ee9960 |
| T23/M8 | wait_for_resources | call_938b1cc7dc474beaadb2dbf6 | NOT_PERSISTED | — | failed | — |
| T24/M9 | get_job | call_8d77e8243e554fe3b9a82ea8 | NOT_PERSISTED | 7337964a-86be-4307-bb09-0848bc0c1744 | succeeded | — |
| T25/M9 | browser_text | call_b9dec6a871694259a89ee5c0 | 79c3895d-33bf-479e-9a00-470f3e9112a7 | cc85a177-af61-469b-899b-00718deb5ed9 | succeeded | d19e2068-9658-4d28-88eb-2f27b98db821 |
| T26/M10 | read_artifact | call_c878c48c30664d739f6b5502 | NOT_PERSISTED | — | succeeded | d19e2068-9658-4d28-88eb-2f27b98db821 |
| T27/M10 | record_observation | call_4737ff6b42824c068e349f51 | 29a05c2a-b03c-4be8-abf7-35ca7e595a02 | — | ok | — |
| T28/M11 | propose_finding | call_97fbee661a1148e6ade1dcd7 | 7a7d0fbc-83b1-4a74-b631-67b28df36ae7 | — | ok | — |
| T29/M11 | propose_finding | call_114a4cad97b149f6aab5e300 | 37cfbd31-e6d6-4de2-8779-126463d6129f | — | ok | — |
| T30/M12 | propose_completion | call_646a1205356547f0be11e3a7 | NOT_PERSISTED | — | ok | — |

### R1 Executor Ledger

| Job ID | Operation ID | kind/runtime | state/exit | 结束时间(+08:00) | 按对应协议重算摘要 |
| --- | --- | --- | --- | --- | --- |
| 700d011c-00b4-4886-9dff-b78b5908223d | 0ac82d3f-aa38-474c-b7f7-73a1d8014736 | http_request/legacy | succeeded/0 | 15:56:24.180 | PASS |
| 2fdc38c1-a22e-4483-86d8-c4924fda4c02 | 0f5485cc-01fe-4c07-9d5b-2daa20ce5dc7 | http_request/legacy | succeeded/0 | 15:55:58.197 | PASS |
| 7337964a-86be-4307-bb09-0848bc0c1744 | 330e07b0-f585-4b76-886c-b3b8fe5c85a3 | browser/legacy | succeeded/0 | 15:57:08.582 | PASS |
| 7dc152fe-60ce-4f04-b1f1-878642e311ea | 46c9c970-687a-433f-a3fa-4719c3f397a0 | http_request/legacy | succeeded/0 | 15:55:47.024 | PASS |
| 067849fb-a578-4841-9957-3fd06c0ea8d0 | 49c10ded-8d1b-4a9d-afb3-d5b7717f3d88 | http_request/legacy | succeeded/0 | 15:55:38.293 | PASS |
| 6b93e6e7-c56c-4296-996e-320033377e7c | 568eb8ac-2425-4d37-8ed6-9f8346e28975 | http_request/legacy | succeeded/0 | 15:55:39.080 | PASS |
| 67491671-216f-4735-889a-523aa1ad15a7 | 5b42372f-2d7f-4bd9-a63f-209685f7ea6e | http_request/legacy | succeeded/0 | 15:55:39.169 | PASS |
| d4401c78-5deb-4b14-b16b-be9e6fa328fc | 6f240e92-7a13-4061-9f8b-9ff948d09291 | http_request/legacy | succeeded/0 | 15:55:33.156 | PASS |
| d8ffc846-ebae-42ae-9020-f8e4f49da3d4 | 6f4c87a4-0199-47c8-9992-ebb24f16bcbb | http_request/legacy | succeeded/0 | 15:55:47.823 | PASS |
| 38141990-3ab0-4626-8082-8316908a9e00 | 77d3f25c-c029-4361-b672-43647273c768 | http_request/legacy | succeeded/0 | 15:55:47.141 | PASS |
| cc85a177-af61-469b-899b-00718deb5ed9 | 79c3895d-33bf-479e-9a00-470f3e9112a7 | browser/legacy | succeeded/0 | 15:57:12.617 | PASS |
| 06d6ba4b-d473-463d-a6b9-3ce380626c73 | 79ce3117-bdcc-4bc3-ba19-8f6889e54d89 | command/execution_v1 | succeeded/0 | 15:55:48.278 | PASS |
| 28f8eeed-6c91-4fdc-9c04-c78660e97f87 | 892d2688-e51e-4bcc-873c-47604ff9a548 | http_request/legacy | succeeded/0 | 15:55:38.384 | PASS |
| c1563b0c-239f-448b-bd1d-dae15547de10 | 8c1ac77a-341d-44d4-845b-f097c0ea6241 | http_request/legacy | succeeded/0 | 15:55:57.479 | PASS |
| 1470afdf-48da-462c-a68f-219a76b4316b | afa8b9e6-5954-4dfd-b1de-0da8f50c60c5 | http_request/legacy | succeeded/0 | 15:55:57.540 | PASS |
| 1f7a075a-9f2b-4d26-b458-e4dbe21cc5b0 | bbb4cda9-b92c-4086-ac9d-3fc8854b0562 | http_request/legacy | succeeded/0 | 15:56:24.807 | PASS |
| 6dec0541-0720-4234-b7b0-c57db072735c | c40e99d5-f9c4-4078-9855-018c2a49beba | http_request/legacy | succeeded/0 | 15:56:37.559 | PASS |
| 3dd175f9-fc5b-459b-af48-ccd874fcb637 | c6b7388a-9f4a-4a33-a5b7-469aba3c32dc | http_request/legacy | succeeded/0 | 15:55:58.167 | PASS |
| 7fcf2f52-d9c9-420a-bdac-f2e43aae4ce5 | d2a28b43-12ab-46bd-b732-4a4e5db0b4bd | http_request/legacy | succeeded/0 | 15:55:38.965 | PASS |
| 8b2a305f-1bcd-4866-af82-ea74036cb3e2 | e5d675ad-2baa-4901-b766-c28eaf40a98e | http_request/legacy | succeeded/0 | 15:56:24.211 | PASS |
| 4a0c222d-35a7-434d-b7a5-f765216a7b73 | e61d37c0-268c-49f6-8cf3-f2786542f713 | http_request/legacy | succeeded/0 | 15:57:00.864 | PASS |

### R1 正常Segment边界

| session_seq | 时间(+08:00) | checkpoint_id | 已完成决策数 |
| --- | --- | --- | --- |
| 67 | 15:56:00.267 | 1f1cdee0-56f4-63c2-8026-4142e374b294 | 4 |
| 123 | 15:57:09.439 | 1f1cdee2-eaa7-6879-804e-835c28049380 | 8 |
| 171 | 15:58:00.401 | 1f1cdee4-d0a6-6e01-8076-51f0674b615e | 12 |

### R2 模型请求

| 序号 | ModelCall ID | LogicalCall ID | 提交→完成(+08:00) | 供应商input/output/cache/reasoning | Context Artifact |
| --- | --- | --- | --- | --- | --- |
| M1 | 9de02db5-73e1-466c-af90-2052fab31143 | 86e5cf67-22ed-8f52-5cbf-1ea3bfd961bb | 16:00:57.011 → 16:00:59.716 | 15721/86/15360/NOT_AVAILABLE | 6ba45bc9-b297-4885-b854-33527cd18fdd |
| M2 | 17eb27d0-6cff-41f7-8d98-23b0b75510c1 | 7a41fb1b-b4e5-1cdc-d021-1956fd78824c | 16:01:00.852 → 16:01:06.639 | 16873/364/15616/105 | 78cee733-c9cc-48c7-bdc1-16dbbe4b4cfe |
| M3 | 20b5d40e-ab4f-4579-8c50-ff6024ac8b81 | 18058551-4da8-c6b8-da6c-67e02d3e8a4e | 16:01:13.049 → 16:01:22.214 | 19263/717/18432/380 | 6fe04127-b926-4bc2-80c6-220b92cbdda1 |
| M4 | 064b9de7-17b8-4356-b1fe-91f2a3c23445 | 16d92a89-e373-25fb-d09c-8c179e107437 | 16:01:24.640 → 16:01:32.139 | 23181/615/22528/380 | 69ea203e-9706-4260-81c8-f945c63a43ea |
| M5 | 39637194-f4ad-4e65-9fe4-46f76bfade87 | 631424d8-56dd-8ba4-97b1-ff1fbd639de1 | 16:01:34.141 → 16:01:39.260 | 24779/480/24576/218 | d6c99efb-d660-424d-89cf-1e467780127e |
| M6 | bc17683c-533f-4915-ac9c-9642f58b8386 | 79f2bacb-9c4b-b5c1-4d68-5623f938b5fc | 16:01:40.535 → 16:01:56.930 | 26571/1164/25600/165 | 833124e5-ee3c-4201-812d-db8f2a4c6993 |
| M7 | 3e6a57b9-e5ba-4b39-b745-af69e430a762 | 83c1da28-b30a-132e-f275-5b5803be780e | 16:01:57.239 → 16:02:11.624 | 28331/1348/27648/3 | 96d9d776-5315-43ac-a28c-3c2e6daf8f38 |
| M8 | a163c9d0-f5ac-472c-8692-8ad7276c6bca | 187f1432-ae1f-2d4c-9480-bf9e6e78c53f | 16:02:11.956 → 16:02:17.114 | 31088/273/30720/146 | 5d962d37-7f24-4053-a363-fdc32c55d611 |
| M9 | 6af9230a-b048-407f-b876-694133d060eb | db014256-781f-39c7-d5b5-15197ff2f4c4 | 16:02:17.488 → 16:02:24.694 | 34959/503/34816/373 | a0204845-3640-4c98-95bd-8802aad1dab7 |
| M10 | b13fe6f7-0d09-47c2-953e-fc52441851d5 | 4d165c17-9303-79f1-b479-d3116e6b49d5 | 16:02:25.765 → 16:02:42.606 | 36514/1607/34816/56 | ec79dcc3-940a-431e-822d-abb59c1fb062 |

### R2 工具调用

| 序号 | 工具 | ToolCall ID | Operation ID | Job ID | ToolMessage结果 | Artifact引用 |
| --- | --- | --- | --- | --- | --- | --- |
| T1/M1 | http_request | call_0a9dd4af937f4d5caad08089 | 24c5ccf3-f15a-470f-9599-af992daaf5bb | 357b6215-bb01-4c0d-abff-e59497072f10 | succeeded | d493f2dd-d1ae-483d-9b6c-cfc743ea7b87, d67e070c-70b5-4193-8bc9-57c1fafa9ef1 |
| T2/M1 | session_info | call_39c3b2997972423fa9e4e274 | NOT_PERSISTED | — | succeeded | — |
| T3/M2 | http_request | call_2b5e084f68bc41358d610531 | bca5d0fb-0235-463b-b17a-740fa2785479 | 064a36c7-9e6b-4e69-b0e2-5022200b50a7 | succeeded | 0eaa62cc-fbc6-4a19-85dc-9e138a02cc4f, 205f8316-b743-4703-8cc4-0e310a4bd8e1 |
| T4/M2 | run_command | call_8a86955ae5bc43538a712164 | 7ce93f16-7f07-4491-86f1-a15a67841cec | 318068a6-c6b2-4c57-becf-7d3cb7f62f99 | succeeded | — |
| T5/M3 | run_command | call_c6085a26dec34a57a1f008e3 | 49ef83bc-cb8e-43c9-9d03-12c6ea0713a5 | 2d406153-7085-459a-b62c-37e7b7fc8b70 | succeeded | — |
| T6/M4 | run_command | call_a6d5240d053f43cdbba3d86a | 415422f8-e8ce-46ac-a332-02b7ae9785bc | 583b9283-8f17-4caa-a55b-702cf68c6111 | succeeded | — |
| T7/M5 | http_request | call_39a4a3c263914d39b475eae7 | f2e87853-5289-41dd-b569-ec5714d7bc72 | 20d82807-2720-4995-9bd1-09823d5fbbf3 | succeeded | 39cfccba-37b1-405d-b5b8-079976239bdf, 2c3fa2e1-e10a-4a12-97d6-c4502f28d8de |
| T8/M5 | export_execution_output | call_104b7ef498d047159b92ff57 | NOT_PERSISTED | 583b9283-8f17-4caa-a55b-702cf68c6111 | succeeded | 8b52c566-0f88-559d-b923-b7792b7f7725 |
| T9/M6 | propose_finding | call_0199fb19064745a5be26137d | 368c09cc-019a-4dc7-83cd-ddf45f348150 | — | ok | — |
| T10/M6 | record_observation | call_457484cecf974cabac8db050 | 739ab682-a142-49c6-b924-22c460584184 | — | ok | — |
| T11/M7 | propose_completion | call_dc6f74c09209412782f3cc38 | NOT_PERSISTED | — | ok | — |
| T12/M8 | search_evidence | call_f3cdf23ff6e64f12b889fd78 | NOT_PERSISTED | — | succeeded | — |
| T13/M8 | read_artifact | call_439d15f595b44c75bd98e42c | NOT_PERSISTED | — | succeeded | 8b52c566-0f88-559d-b923-b7792b7f7725 |
| T14/M9 | http_request | call_608dc3a6de9f4cc999081156 | fb34e1eb-c232-42ca-971a-9f9088caea56 | dc863445-1dff-44e9-b218-b0905eac817a | succeeded | b8436798-8ac3-4d48-9ce0-be442b60b566, 4473412c-6a48-41df-b4c3-09a42ba61d55 |
| T15/M10 | propose_completion | call_239d53fe0c4b4e64b690a93f | NOT_PERSISTED | — | ok | — |

### R2 Executor Ledger

| Job ID | Operation ID | kind/runtime | state/exit | 结束时间(+08:00) | 按对应协议重算摘要 |
| --- | --- | --- | --- | --- | --- |
| 357b6215-bb01-4c0d-abff-e59497072f10 | 24c5ccf3-f15a-470f-9599-af992daaf5bb | http_request/legacy | succeeded/0 | 16:00:58.710 | PASS |
| 583b9283-8f17-4caa-a55b-702cf68c6111 | 415422f8-e8ce-46ac-a332-02b7ae9785bc | command/execution_v1 | succeeded/0 | 16:01:32.478 | PASS |
| 2d406153-7085-459a-b62c-37e7b7fc8b70 | 49ef83bc-cb8e-43c9-9d03-12c6ea0713a5 | command/execution_v1 | succeeded/0 | 16:01:22.949 | PASS |
| 318068a6-c6b2-4c57-becf-7d3cb7f62f99 | 7ce93f16-7f07-4491-86f1-a15a67841cec | command/execution_v1 | succeeded/0 | 16:01:11.405 | PASS |
| 064a36c7-9e6b-4e69-b0e2-5022200b50a7 | bca5d0fb-0235-463b-b17a-740fa2785479 | http_request/legacy | succeeded/0 | 16:01:05.646 | PASS |
| 20d82807-2720-4995-9bd1-09823d5fbbf3 | f2e87853-5289-41dd-b569-ec5714d7bc72 | http_request/legacy | succeeded/0 | 16:01:38.386 | PASS |
| dc863445-1dff-44e9-b218-b0905eac817a | fb34e1eb-c232-42ca-971a-9f9088caea56 | http_request/legacy | succeeded/0 | 16:02:23.696 | PASS |

### R2 正常Segment边界

| session_seq | 时间(+08:00) | checkpoint_id | 已完成决策数 |
| --- | --- | --- | --- |
| 48 | 16:01:33.987 | 1f1cdeec-c599-6231-8026-a763b0ce2ac1 | 4 |
| 102 | 16:02:17.343 | 1f1cdeee-6316-6aee-804e-8f5aeb17fcd2 | 8 |

### R3 模型请求

| 序号 | ModelCall ID | LogicalCall ID | 提交→完成(+08:00) | 供应商input/output/cache/reasoning | Context Artifact |
| --- | --- | --- | --- | --- | --- |
| M1 | dfc229b5-0a90-4cef-940f-a95d8b77648d | 0cdf6508-b86e-ac5d-0750-58cae4cdb35d | 16:04:29.024 → 16:04:31.644 | 15721/147/15360/NOT_AVAILABLE | 30a299c8-b21e-4d3e-bb66-334236802777 |
| M2 | 97b12529-e6a5-49ed-b54c-c86afa0cd619 | 30dabcb7-091e-ae36-9a9a-850eee487411 | 16:04:33.716 → 16:04:50.809 | 18452/1517/18432/970 | 13a07aef-7874-4158-9f1b-9e1c8ad958af |
| M3 | 6df414df-a291-4852-972c-ebe28b0ce9e1 | 9eff379f-3089-f8b9-0753-62ab40e7c9b6 | 16:04:52.952 → 16:05:03.219 | 21458/712/20480/271 | 96a2c93e-b948-4988-8a98-ca362d0841c1 |
| M4 | a45d87cd-454a-47dd-8d71-3bf681e2755e | f42d0028-c90a-664b-e6a1-07254502b6f1 | 16:05:05.385 → 16:05:13.149 | 23122/704/22528/232 | 89422f9a-d7cf-4b71-bb01-e8cdfdb04dd5 |
| M5 | 99401302-f364-46e1-98e5-482b38e93db2 | 4471230d-d38e-3d8a-a182-bdde9712efba | 16:05:15.142 → 16:05:18.904 | 24836/260/24576/27 | 98ad0ae3-ea40-46d5-8271-db1ff7bc755c |
| M6 | c0781404-d559-41af-b8ff-239a94f7f305 | 748ad288-9f03-3a89-1230-6cd31257f933 | 16:05:19.311 → 16:05:29.160 | 25752/777/25600/49 | 041b68d8-65e5-4518-830e-6d3bd7b755ec |
| M7 | 24c69a31-4dd7-41e0-81e2-147d72b458dc | 17fbbb1d-eb94-6e93-efd7-158b7ebacd67 | 16:05:29.461 → 16:05:47.048 | 27162/1575/26624/66 | 62d7ebd7-ca0a-489b-82ba-d7c260ae0be1 |
| M8 | e02f16bc-c6d8-4527-b16a-69500c5df338 | 3ad55754-7426-1037-a236-e29121f0ec3b | 16:05:47.418 → 16:05:54.125 | 30328/337/29696/128 | 9d25a6ca-95ac-42b8-beed-f655164a5552 |
| M9 | c335854b-8dc6-41c6-92bc-2abe9ce5f9c1 | 6449d671-3102-ecbb-951d-6d4e7f51e9b2 | 16:05:54.569 → 16:06:23.295 | 34963/2570/34816/1819 | 99185153-f159-47ef-a4d2-a60176beabf2 |
| M10 | 50db69b8-7013-4d79-83df-78c37a27ec68 | 5b5ebc64-5412-cfcd-0092-e7ac093a342b | 16:06:25.081 → 16:06:36.294 | 38799/695/37888/270 | 9e617c70-914c-45ef-ae4d-56d652f051b2 |
| M11 | 5efec662-dac5-4c59-b4ca-dbba15361a29 | 205bf978-8beb-6eda-f637-1f804b3672c8 | 16:06:37.539 → 16:06:43.070 | 40481/255/39936/89 | a4fded05-c05b-46f6-a8fe-87c274a6d4f6 |
| M12 | 02816563-f15f-4001-b637-d71ae6c009ec | 0468cbe1-8437-21c9-812b-b47174dd24d8 | 16:06:43.382 → 16:07:03.857 | 41946/1080/40960/663 | 5ebdd434-fdf1-40a1-b925-71f433ae6f0d |
| M13 | 0b9cde51-324d-4d1b-97ed-8d46722494c3 | 20004bb6-bf7e-9416-415e-1f5f45d207bc | 16:07:04.339 → 16:07:23.186 | 43181/1892/43008/141 | e3128702-c20d-44fd-a394-ba9959138c03 |

### R3 工具调用

| 序号 | 工具 | ToolCall ID | Operation ID | Job ID | ToolMessage结果 | Artifact引用 |
| --- | --- | --- | --- | --- | --- | --- |
| T1/M1 | http_request | call_8ce8e73d9913406ab5ef0e07 | c50de296-0d18-4163-a7e2-14cf4ab79f92 | 17295784-f5ac-435b-9831-ed2ba1a0a3b9 | succeeded | fabbc886-0bac-4aa1-99ff-3b3d487f811f, f0802875-474d-4a6b-8d4a-18f681d8ab23 |
| T2/M1 | run_command | call_0cf64bab13044421a1d998ea | 24bedb77-03b1-4346-9489-52323368d08a | 25407b98-cf1d-4180-b134-f21f474288f3 | succeeded | — |
| T3/M2 | http_request | call_3d9cb02905034dccb91b323b | 2d11f08f-f1b0-426a-b92a-61401630275b | 4b0af2b4-ccbe-4091-8d0b-123d7b27e76d | failed | — |
| T4/M2 | run_command | call_b8a21e9fba8e44aaa57fa313 | e25975bf-24d5-44ba-937a-49048b68fa98 | d696df2c-6f9e-4627-ade6-354becce5712 | succeeded | — |
| T5/M3 | run_command | call_e02ae31692c440f69f8bb927 | 1be851d9-128f-4d8a-9584-889a53de99dc | 2042bbb1-da1c-4ac5-82a5-6af3f9c2d8f1 | succeeded | — |
| T6/M4 | run_command | call_a5ec7db489324ea199f79dc8 | d92ebe06-74e4-48e2-8ba8-23b4f1cf3911 | fe7715e9-6c33-4ce6-a4f4-4818c17faf98 | succeeded | — |
| T7/M5 | export_execution_output | call_25ff11d427fd4b6c8958fdc6 | NOT_PERSISTED | 2042bbb1-da1c-4ac5-82a5-6af3f9c2d8f1 | succeeded | da2077eb-f3d6-5a62-a214-4d7d9e3438f6 |
| T8/M5 | export_execution_output | call_2cb73b9bdf5842c396e80803 | NOT_PERSISTED | fe7715e9-6c33-4ce6-a4f4-4818c17faf98 | succeeded | 57614e6d-055d-58b7-b5e6-d52e427a07ba |
| T9/M6 | propose_finding | call_dfddf8fbc0a54ba4873d18de | 13779c82-cdff-4fb8-b09c-5a41b4032322 | — | ok | — |
| T10/M6 | record_observation | call_131eaf1222c14535aa4196f1 | e4b1ef4a-510a-454b-86ff-96841152fd7a | — | ok | — |
| T11/M7 | propose_completion | call_ca083371d6894a3984253cfd | NOT_PERSISTED | — | ok | — |
| T12/M8 | search_evidence | call_a7918732f36345c29a9cbc00 | NOT_PERSISTED | — | succeeded | — |
| T13/M8 | read_artifact | call_cbe5914a32a2483ba7d11f6e | NOT_PERSISTED | — | succeeded | da2077eb-f3d6-5a62-a214-4d7d9e3438f6 |
| T14/M8 | read_artifact | call_7e1dcb95b6254b09a612d229 | NOT_PERSISTED | — | succeeded | 57614e6d-055d-58b7-b5e6-d52e427a07ba |
| T15/M8 | read_artifact | call_ff96268304294329b2aefa56 | NOT_PERSISTED | — | succeeded | fabbc886-0bac-4aa1-99ff-3b3d487f811f |
| T16/M9 | run_script | call_02e8e94e0f6e4eb5897711dc | bcd0f1fa-6697-4d6b-b0dd-3a6e1868c8bf | c1d8d802-d213-4267-89ed-16216d486d4b | succeeded | — |
| T17/M10 | http_request | call_e361c8e8213842eb91c0a745 | 801bf2b4-c7fb-4ae3-b8bb-feaea75ef940 | 6de5c291-cc0b-4da2-94ea-9ae04c40eb53 | succeeded | 50b58c50-5832-413a-aa15-ca50a58f723d, 238de3e5-3561-4898-93e3-6eec2e6efb67 |
| T18/M11 | read_artifact | call_5bd3baf5d9444c349458497b | NOT_PERSISTED | — | succeeded | 238de3e5-3561-4898-93e3-6eec2e6efb67 |
| T19/M11 | export_execution_output | call_b8859637b5ae4f83bc054a14 | NOT_PERSISTED | c1d8d802-d213-4267-89ed-16216d486d4b | succeeded | d0e1dbc9-af79-52f1-b16e-d0d6dbe410cd |
| T20/M12 | record_observation | call_822acd1a008c45feba8d4679 | 39824902-411c-4a3b-8ed2-79b103651258 | — | ok | — |
| T21/M13 | propose_completion | call_e501bb6f7100442787f4c2a0 | NOT_PERSISTED | — | ok | — |

### R3 Executor Ledger

| Job ID | Operation ID | kind/runtime | state/exit | 结束时间(+08:00) | 按对应协议重算摘要 |
| --- | --- | --- | --- | --- | --- |
| 2042bbb1-da1c-4ac5-82a5-6af3f9c2d8f1 | 1be851d9-128f-4d8a-9584-889a53de99dc | command/execution_v1 | succeeded/0 | 16:05:03.706 | PASS |
| 25407b98-cf1d-4180-b134-f21f474288f3 | 24bedb77-03b1-4346-9489-52323368d08a | command/execution_v1 | succeeded/0 | 16:04:32.023 | PASS |
| 4b0af2b4-ccbe-4091-8d0b-123d7b27e76d | 2d11f08f-f1b0-426a-b92a-61401630275b | http_request/legacy | failed/None | 16:04:49.992 | PASS |
| 6de5c291-cc0b-4da2-94ea-9ae04c40eb53 | 801bf2b4-c7fb-4ae3-b8bb-feaea75ef940 | http_request/legacy | succeeded/0 | 16:06:35.473 | PASS |
| c1d8d802-d213-4267-89ed-16216d486d4b | bcd0f1fa-6697-4d6b-b0dd-3a6e1868c8bf | script/execution_v1 | succeeded/0 | 16:06:24.763 | PASS |
| 17295784-f5ac-435b-9831-ed2ba1a0a3b9 | c50de296-0d18-4163-a7e2-14cf4ab79f92 | http_request/legacy | succeeded/0 | 16:04:30.684 | PASS |
| fe7715e9-6c33-4ce6-a4f4-4818c17faf98 | d92ebe06-74e4-48e2-8ba8-23b4f1cf3911 | command/execution_v1 | succeeded/0 | 16:05:13.398 | PASS |
| d696df2c-6f9e-4627-ade6-354becce5712 | e25975bf-24d5-44ba-937a-49048b68fa98 | command/execution_v1 | succeeded/0 | 16:04:51.338 | PASS |

### R3 正常Segment边界

| session_seq | 时间(+08:00) | checkpoint_id | 已完成决策数 |
| --- | --- | --- | --- |
| 51 | 16:05:14.983 | 1f1cdef5-012f-65b2-8026-6ab006871de7 | 4 |
| 106 | 16:05:54.411 | 1f1cdef6-793b-62f2-804e-b969aaa6a317 | 8 |
| 158 | 16:07:04.167 | 1f1cdef9-1277-6186-8076-a9b4b1f31c44 | 12 |

## E8 Runner日志

原文件：`C:/xjs/rexak-agent/data/runtime-precontract-20261021/logs/launcher/runner.log`。以下为有界定位，不复制供应商地址、认证或全部HTTP日志。

读取时SHA-256：`c121358ef26b5f6fc2e293550a9c35dd1ca0bafd086a4477910c727c6f2d1f43`。

- L21: {"prompt_version": "12", "code_version": "loaded-minimal-loop:09ca90ff7aad07ecd99ed886ff3439185521f1d9", "scope": "loaded_minimal_loop_entrypoints_and_prompt_not_entire_process", "event": "runner_runtime_versions", "level": "info", "timestamp": "2026-10-22T07:54:23.232629Z"}

- L375: 2026-10-22 15:58:00 [info     ] run_memory_distilled           candidates=0 run_id=e2856917-9842-4685-a9fa-79ba45ad4b9a

- L489: 2026-10-22 16:02:43 [info     ] run_memory_distilled           candidates=1 run_id=dadda09c-ce80-4553-8cb0-cf6676e11796

- L630: [conn=3] Sending disconnect: Max packet size exceeded (2)

- L636: 2026-10-22 16:07:23 [info     ] run_memory_distilled           candidates=2 run_id=cfc0e9c6-433e-4e17-b22e-df92a54b81b0

启动startup_recovery日志位于Run创建前，runs_marked_recovering、jobs_reconciled等均为空；不能把它当三次中的恢复演练。

## E9 源码位置与读取时hash

文件均相对 `C:/xjs/rexak-agent/`。这些是本次读取位置；加载指纹范围与历史执行证据见E0，不将当前源码当作全进程历史字节码证明。

| 引用 | 源码位置 | 支持内容 | SHA-256 |
| --- | --- | --- | --- |
| S01 | src/agent_workbench/agent/factory.py:121 | 官方create_agent装配、可选禁用旧节点、摘要中间件 | eca0d36dc4a3ce793fa96defde6b4c15aa8f37ae268eab5251572becf95ae5f1 |
| S02 | src/agent_workbench/contracts/plan.py:10 | 轻量计划patch，无CompletionSpec | adfc1ae548039b744bcb555187dd73556e66f2cf258393fd1d8f5a6b091bfd5e |
| S03 | src/agent_workbench/tools/planning.py:51 | 局部更新与expected_plan_revision | 972282831a799a3ee590597923478bd3564d0c094af0c6a7ca1355b02875c33d |
| S04 | src/agent_workbench/agent/middleware/working_context.py:11 | 只读黑板与可选Focus | c39724cfe69a857ac609b8552f637123011c2749df245e936c5f3017f49b7e96 |
| S05 | src/agent_workbench/tools/registry.py:61 | 默认关闭专用Verification，旧工具代码存在不代表启用 | 11259e5f4371ad096f09ab776a9007c48ec09d4edb3311c9e653f4a60bfc2c4d |
| S06 | src/agent_workbench/runtime/stopping.py:293 | 引用检查包含source_operation_id、同会话、可用、未截断和hash | 7de744c99a44b44c60414d7017f3d7d3566899ceabeef1e68bd960900a840a7b |
| S07 | src/agent_workbench/tools/jobs.py:529 | 导出不可变日志，source_operation_id=None | 886c33a8806ac4cc7aa28d263c6db33f1a854a62d70e374474cfaef93c21f1b6 |
| S08 | src/agent_workbench/tools/execution_resources.py:216 | 等待只解析会话execution resource集合 | 5d7a817670138968c30c5106632c9d4b8b164aaee2c01f3297ff650d784989d1 |
| S09 | src/agent_workbench/models/context_budget.py:51 | 按wire估算system/messages/tools/回传字段/协议，不是供应商tokenizer | e3894351e7d1ae93a54b3c4eb66050a3303693e6313332323e72eb0dcfd225f8 |
| S10 | src/agent_workbench/agent/middleware/context_builder.py:30 | 目标重复投影及预算/manifest | 06c834729ce5917d18a6a6d70ba1a42d38bfc9001e3973c484ace3b8fa92d31b |
| S11 | src/agent_workbench/models/invocation.py:488 | 网络前保存manifest、context.selected与ModelCall关联 | 11ba592bbb420ea9fde043530ca66dd69977cddebc17d7ee63e7cbeb3ee6d212 |
| S12 | src/agent_workbench/agent/middleware/finish_gate.py:72 | 拒绝后系统反馈与finish_gate_attempts | 0d7ff27a593770f0e35307c6876776eca391eda0ba03e73b05870fc0713d679b |
| S13 | src/agent_workbench/tools/task.py:454 | 可选Observation、来源检查及自动Asset关联 | c3413c523c289b03847c48a665f872b1d8e1d2d5669079a48375bac9814526ec |
| S14 | src/agent_workbench/agent/middleware/compaction.py:43 | 摘要存在，但三次未触发 | 31a03f566254b28c9c49937696e2357d93ee97ed1a6f1ac204f74479e8315572 |
| S15 | src/agent_workbench/runtime/runner.py:885 | 阶段末业务记忆投影，非额外模型调用 | 5e7153da2244150a865e57c67df8884941de1acca7d5f9823b302313c4578bde |
| S16 | src/agent_workbench/agent/prompts/system.py:3 | 提示12，普通动作无需Plan/Lead/Assessment/Coverage | 67fcac11b2a947a67b7234991e9ce7b169efbf1e60f1461c4bae71bd9f918c23 |
| S17 | src/agent_workbench/agent/state.py:70 | 兼容completion_spec等历史字段仍存在 | 895bb96ed3ff7751e1f96e3d2650ad0ff7b229f2b879fa91457d8ae88b42d861 |
| S18 | src/agent_workbench/agent/focus.py:133 | 历史Assessment辅助函数尚存，不能推断生产强制启用 | 8766d844025c92d2c974bb9f63f4e052b9292b5722ba1ef46a3cc3a30478959b |
| S19 | docs/decisions/0014-minimal-autonomous-loop.md:3 | 当前重构决策；效果仍以本次记录为准 | 1426939627eca6413fdf02186d3ca99714aa9b5e27243b3620c72b1329251439 |
| S20 | src/agent_workbench/maintenance/diagnostics.py:66 | manifest estimated_tokens顶层被脱敏，但budget.components保留 | f12b011eb605ecf1f82df43d002f3263dc597647a15f4678e354afdebc0934a3 |
| S21 | src/agent_workbench/contracts/execution_runtime.py:31 | 命令执行线协议摘要与业务摘要分开 | d1c1bbde2ae632fd03575026e24479b974b7f966c0c47d3dc576aa3fe5cd4dd2 |
| S22 | src/agent_workbench/tools/execution_dispatch.py:166 | 稳定Operation身份与独立线协议摘要 | 24033974fb5d086940e7cd593bcc95f57df2e0a67c7674ef8245ecf096196037 |
| S23 | src/agent_workbench/tools/browser.py:312 | 浏览器摘要使用browser包结构 | 9e11a80be7232a47e6c7f9bd3fcbd31a69f1230043a90db8001c18371a8be8ee |
| S24 | src/agent_workbench/tools/http.py:383 | HTTP摘要输入与派发payload字段不同，默认TLS兼容处理 | 9ed5f06b0a9ddbae8e9a126251493da5833862c8160d67f7435419d6d3383fd6 |

## E10 统计复核口径

`benchmark-metrics.csv`每Run一行，UTF-8无BOM。reasoning_tokens=NOT_AVAILABLE同时保留reasoning_reported_subtotal与缺失调用数；actual_api_cost=NOT_AVAILABLE、cost_status=unpriced。内部工具包含证据读回而不仅是领域写。first_goal_time对R1不可精确定义，另给first_rce证据区间。模型/工具前后计数采用完整决策批边界。

只读核对查询形状（参数为E0的ID，不含连接凭据）：

```sql
BEGIN READ ONLY;
SELECT * FROM app.runs WHERE id = ANY(:run_ids);
SELECT * FROM app.commands WHERE session_id = ANY(:session_ids);
SELECT * FROM app.model_calls WHERE run_id = ANY(:run_ids);
SELECT * FROM app.tool_operations WHERE run_id = ANY(:run_ids);
SELECT v.* FROM app.verifications v JOIN app.findings f ON v.finding_id=f.id
WHERE f.session_id = ANY(:session_ids);
-- checkpoint通过只读Saver连接加载，不做setup/迁移。
ROLLBACK;
```

SQLite jobs中legacy HTTP/浏览器的session_id为空，故按控制端Operation ID关联核对；未错误地只按远端session_id统计。远端operations表只有9条新执行命令/脚本receipt、状态accepted，终态以jobs及completion记录核对，不能把accepted receipt误报为9个未完成执行。
