// 从设计稿原型里抠出纯几何求值层，生成 Python 侧对拍用的夹具。
//
// 只取「几何原语 + 场景构造 + 单次求值」这一段：给定道路尺寸与偏移求最小净距，
// 不含任何优化或求解。这一层 Python 必须与原型逐位一致，也正是移植最容易抄错的地方。
//
//   node scripts/export_scenario_fixtures.mjs <设计稿.dc.html> > tests/fixtures/scenario_clearance.json

import fs from 'node:fs';

const source = fs.readFileSync(process.argv[2], 'utf8');
const from = source.indexOf('// ---------- geometry primitives ----------');
const to = source.indexOf('// ---------- offsets ----------');
if (from < 0 || to < 0) throw new Error('原型结构已变，找不到几何求值段');
const body = source.slice(from, to);

const build = new Function(
  'R', 'Lf', 'Lr', 'hw', 'type', 'bidir', 'gear',
  `${body}\nreturn { evalCfg };`,
);

const VEHICLE = { R: 1.6, W: 1.23, Lf: 1.545, Lr: 2.223 };
const ARC = 0.015;
const LIN = 0.06;

const DIMS = [
  { wA: 2.6, wB: 2.6, wV: 2.7, wH: 3.0, LS: 3.0, w: 2.4, b: 1.2, D: 3.6 },
  { wA: 3.0, wB: 3.0, wV: 3.0, wH: 3.5, LS: 4.0, w: 3.0, b: 1.0, D: 4.5 },
  { wA: 3.4, wB: 3.2, wV: 3.3, wH: 3.8, LS: 4.6, w: 3.2, b: 0.9, D: 5.0 },
  { wA: 4.2, wB: 3.6, wV: 3.8, wH: 4.4, LS: 5.2, w: 3.6, b: 0.8, D: 6.0 },
  { wA: 5.5, wB: 5.0, wV: 5.0, wH: 5.5, LS: 6.5, w: 4.5, b: 1.4, D: 7.5 },
  { wA: 3.0, wB: 3.0, wV: 3.0, wH: 3.5, LS: 4.0, w: 1.8, b: 0.6, D: 4.5 },
];
const OFFSETS = [
  {},
  { eA: 0.2, eB: -0.15, eV: 0.1, eH: -0.2, a: 0.25, so: -0.1, e1: 0.3, e2: 0.15, eo: -0.2, yc: 0.4 },
  { eA: -0.35, eB: 0.4, eV: -0.3, eH: 0.35, a: -0.2, so: 0.3, e1: -0.25, e2: -0.4, eo: 0.5, yc: -0.6 },
];
const ZERO = { eA: 0, eB: 0, eV: 0, eH: 0, a: 0, so: 0, e1: 0, e2: 0, eo: 0, yc: 0 };

const cases = [];
for (const type of ['corner', 'crossback', 'stubback', 'uturn']) {
  for (const bidir of [false, true]) {
    for (const gear of ['D', 'R']) {
      if ((type === 'crossback' || type === 'stubback') && gear === 'R') continue;
      const { evalCfg } = build(VEHICLE.R, VEHICLE.Lf, VEHICLE.Lr, VEHICLE.W / 2, type, bidir, gear);
      for (const dims of DIMS) {
        for (const raw of OFFSETS) {
          const off = { ...ZERO, ...raw };
          const out = evalCfg(dims, off, ARC, LIN, false);
          cases.push({
            type, bidir, gear, dims, off,
            arc_step: ARC, line_step: LIN,
            clearance: out.clear,
            buildable: !out.B.bad,
          });
        }
      }
    }
  }
}

process.stdout.write(JSON.stringify({ vehicle: VEHICLE, cases }, null, 1) + '\n');
