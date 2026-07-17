# Stock Sieve（筛股魔方）

Multi-Personality Evolutionary Stock Selection Engine.

## 协议栈

| # | 文件 | 状态 |
|---|------|------|
| 1 | `docs/specs/agent_contract_v1.1.md` | ✅ Frozen |
| 2 | `docs/specs/personality_genome_schema_v3.2.yaml` | ✅ Frozen |
| 3 | `portfolio_policy_schema_v1.1.1.yaml` | ✅ Frozen |
| 4 | `evaluation_db_ddl_v2.sql` | ✅ Frozen |
| 5 | `docs/specs/evolution_engine_spec_v1.yaml` | ✅ Frozen |

## 架构

```
Research Agent → Portfolio Agent → Execution Agent
       │               │
       └── Evolution Engine ──┘
```

## 快速开始

```bash
pip install -e .
stock-sieve --help
```
