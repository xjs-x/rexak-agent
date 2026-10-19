# 0010：报告存在多个冻结清单时必须显式选择

日期：2026-10-19。状态：已实现，隔离报告与 profile 专项通过。

报告默认读取注册表，只有一组冻结 profile 时才自动采用。存在多个 profile / version / condition_fingerprint 组合时返回 `multiple_checklist_profiles`，不给合并比例，也不按时间挑最新一组。

显式选择仍须命中已登记定义且 hash 一致；选择合法时只计算该组清单。没有登记时仍显示 profile_not_registered。读取不登记、不回填 hash，也不从 coverage 行反推类别。

新增 ambiguous / selected 两个反例。修复前默认多组路径误选最新，显式路径通过；修复后两者通过。该行为只改变报告的分母选择，不修改清单代数或数据库 schema。
