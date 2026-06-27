# Foliation CFT Chip — Data & Results Release **v0.2.0**

**SC / HEA 计算产物** + **多源数据收割管线** + **CFT / FPU / QE 加速芯片网表**（sky130 流片结果）。

> **本包不含**：GCU、NLP 阵列、Yosys/OpenROAD/`foliation-eda` 等 EDA 工具链、完整 Rust 引擎源码。  
> 复现计算需自备 [Foliation-Engine](https://github.com/) 主仓与 `foliation-pass-v1` 环境。

**作者 / Author:** Shan Yu

---

## v0.2.0 更新（2026-06-25）

| 项目 | 内容 |
|------|------|
| **数据来源** | MP（Materials Project API）、JARVIS 2D、OQMD、**COD**（晶体学开放数据库）、OPTIMADE、AFLOW、NOMAD 交叉索引、Matbench 静态 fallback |
| **COD 修复** | 改用 `el1/el2/nel` 元素检索 + CIF 下载（`formula=` 在 COD API 上常返回空） |
| **AFLOW 修复** | 增加 MoS₂ / NbSe₂ / WS₂ 二硫化物 AFLUX 查询（原 HEA-only `nspecies≥4` 常 0 条） |
| **SC** | 新 run `killer_20260625_215721`（NbSe₂）；FPU batch 3/3，`J_best≈0.104` |
| **Killer 池** | 103 候选 / 51 pseudo_ready；harvest 赢家 **MoS₂** `MoS2_JVASP-664.cif` |
| **验算** | NOMAD tar、CheckCIF、Zenodo FPU 质押包、AiiDA 血缘图 |

---

## 包含内容

| 目录 | 内容 |
|------|------|
| `data/sc/` | FPU batch、MCMC JSON、oracle decode/POSCAR、killer `fpu_feed`、parquet 扫掠 |
| `data/sc/runs/` | 各 killer run 的 `candidate.cif` + `fpu_feed/*.json` |
| `data/sources_manifest.json` | 权威数据来源清单与 by_source 计数 |
| `data/hea/` | 热力学退火 + HEA emergence 全链路 |
| `data/chips/` | **cft** / **fpu** / **qe_accel**：`netlist.aig`、`mapped_sky130.v`、`final.def` |
| `data/forge/` | crystal_forge cycle 证据 |
| `data/oracle/` | Θ* blueprint |
| `tools/` | SC/HEA/FPU 管线 + `chip_data_harvest_pipeline.py` |
| `scripts/` | pass-v1 decode / god-mode / fpu_feed |

### 三颗物理芯片

| Chip | AIG 源 | 角色 |
|------|--------|------|
| **cft** | `extensions_collapsed_v2.aig` | Foliation 叶状坍缩 CFT |
| **fpu** | `extensions_universal_v3.aig` | 14-bus DMFT / Universal AIM |
| **qe_accel** | `fpu_extensions_raw.aig` | QE Hamiltonian 同调加速核 |

---

## 权威数据来源

| 来源 | 用途 | 配置 |
|------|------|------|
| [Materials Project](https://materialsproject.org/api) | 稳定相 / 能带隙筛选 | `MP_API_KEY` in `pass-v1/scripts/data_harvester/.env` |
| [JARVIS-DFT 2D](https://jarvis.nist.gov/) | 二维剥离能筛选 | 无需 key |
| [COD](https://www.crystallography.net/cod/) | 实验晶体 CIF | 元素检索 |
| [OQMD](https://oqmd.org/) | 形成能 / 结构 | REST |
| [AFLOW](https://aflow.org/) | 高通量化合物元数据 | AFLUX |
| [OPTIMADE](https://www.optimade.org/) | MP + OQMD 统一结构 API | MP key for MP provider |
| [NOMAD](https://nomad-lab.eu/) | DFT 原始日志交叉验证 | 公开 query |
| Matbench | API 限流时静态 fallback | GitHub raw |

> **切勿**将 `MP_API_KEY` 提交到 Git。`.env` 已在 `.gitignore` 中。

---

## 快速复现

```bash
export PASS_V1_ROOT=/path/to/foliation-pass-v1
export FOLIATION_ENGINE_ROOT=/path/to/Foliation-Engine

# 1) 多源收割 + killer 排名
bash $PASS_V1_ROOT/scripts/data_harvester/run_harvest.sh --limit 200

# 2) 物理双轨 + FPU batch
bash $PASS_V1_ROOT/scripts/run_physics_with_harvest.sh

# 3) 或从本发布包触发
python3 tools/chip_data_harvest_pipeline.py
```

---

## 数据目录

```text
data/
  MANIFEST.json
  sources_manifest.json
  sc/          # batch 报告 + oracle + runs/<killer_id>/
  hea/
  chips/       # cft | fpu | qe_accel
  forge/
  oracle/
```

---

## 中文说明

本仓库是 **对外发布的证据包**：SC/HEA 计算数据、多源收割脚本、三颗非 GCU 物理芯片 sky130 流片产物。

- **不含**私有 EDA 工具链、GCU、NLP 阵列  
- **含** FPU 14 维 Θ MCMC、HEA emergence、killer decode/POSCAR、验算 provenance 包

---

## License

See upstream Foliation-Engine repository for license terms.
