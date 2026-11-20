import {at, drawAxis, drawSlip, drawSuppSlip, tag} from "./exhibits.js";
import {drawTable, views} from "./register.js";

const state = {document: null, history: [], supplements: [], supplement: null, stage: "zones", tab: 0, roll: "", busy: false};
const navigation = [...document.querySelectorAll("nav [data-view]")];
const form = at("plan-form");
const sample = at("sample");
const kerf = at("kerf");
const exportLink = at("export");
const suppForm = at("supplement-form");
const suppExport = at("supp-export");
const formatter = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "medium"});

async function fetchData(path, method = "GET", body) {
  const options = body === undefined ? {method} : {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message ?? `请求失败 ${response.status}`);
  return data;
}

function notice(text = "", failed = false) {
  const node = at("operation");
  node.hidden = !text;
  node.textContent = text;
  node.dataset.error = String(failed);
}

function archiveControls() {
  const manifest = state.document?.manifest;
  const archived = manifest?.sample === sample.value && manifest?.kerf_mm === Number(kerf.value);
  for (const control of [sample, kerf, at("submit")]) control.disabled = state.busy;
  at("rerun").disabled = !archived || state.busy;
  at("print").disabled = !archived || state.busy || !state.roll;
  if (archived && !state.busy) exportLink.href = `/api/export/${manifest.id}`;
  else exportLink.removeAttribute("href");
  exportLink.setAttribute("aria-disabled", String(!archived || state.busy));
  const supplement = state.supplement?.manifest;
  at("supp-submit").disabled = !manifest || state.busy;
  at("supp-rerun").disabled = !supplement || state.busy;
  if (supplement && !state.busy) suppExport.href = `/api/supplements/${supplement.id}/export`;
  else suppExport.removeAttribute("href");
  suppExport.setAttribute("aria-disabled", String(!supplement || state.busy));
}

function heading() {
  const manifest = state.document?.manifest;
  at("archive-state").textContent = state.busy ? "正在排版" : manifest ? "已归档" : state.document ? "样例预览" : "暂无卷宗";
  at("archive-ref").textContent = manifest?.id.slice(0, 12) ?? "—";
  at("archive-ref").title = manifest?.id ?? "";
  const stamp = at("archive-date");
  stamp.dateTime = manifest?.created_at ?? "";
  stamp.textContent = manifest ? `${formatter.format(new Date(manifest.created_at))} · UTC+08:00` : "";
  const suppMode = state.stage === "supplement" && state.supplement;
  const totals = suppMode ? state.supplement.snapshot.totals : state.document?.snapshot.ledger.totals;
  for (const name of ["length", "piece", "blocked", "kerf", "unused", "delta"]) {
    at(name === "kerf" ? "kerf-total" : name).textContent = totals?.[name] ?? "—";
  }
  const fabrics = [...new Set((state.document?.snapshot.zones.rolls ?? []).map((roll) => roll.fabric))];
  at("fabric-list").replaceChildren(...fabrics.map((name) => {
    const option = tag("option");
    option.value = name;
    return option;
  }));
}

function rollPanel() {
  const data = state.document?.snapshot;
  const rolls = data?.zones.rolls ?? [];
  if (!rolls.some((roll) => roll.id === state.roll)) state.roll = rolls[0]?.id ?? "";
  const picker = at("roll-picker");
  const sourceFocus = picker.contains(document.activeElement);
  picker.replaceChildren(...rolls.map((roll) => {
    const button = tag("button", roll.id);
    button.type = "button";
    button.dataset.roll = roll.id;
    button.dataset.held = String(roll.held);
    button.setAttribute("aria-pressed", String(roll.id === state.roll));
    return button;
  }));
  if (sourceFocus) picker.querySelector("[aria-pressed=true]")?.focus();
  if (state.stage === "supplement" && state.supplement) {
    const book = state.supplement.snapshot.rolls.find((roll) => roll.roll === state.roll);
    const zone = rolls.find((roll) => roll.id === state.roll);
    const pieces = state.supplement.snapshot.placements.filter((piece) => piece.roll === state.roll);
    const slip = {roll: state.roll, fabric: zone?.fabric, width: zone?.width, length: book?.length,
                  pieces, lengths: book?.lengths, held: book?.held,
                  status: book ? `补后余 ${book.remaining} mm · 本卷补裁 ${pieces.length} 片` : ""};
    drawAxis(book, slip);
    drawSuppSlip(state.supplement, state.roll, zone);
    return;
  }
  const slip = data?.slips.find((item) => item.roll === state.roll);
  drawAxis(data?.ledger.rolls.find((roll) => roll.roll === state.roll), slip);
  drawSlip(slip, state.document?.manifest, data?.fit.kerf_mm);
}

function recordsPanel() {
  suppForm.hidden = state.stage !== "supplement";
  const tabs = views(state.stage, state.document?.snapshot, state.roll, state.history, state.supplement, state.supplements);
  at("view-title").textContent = navigation.find((button) => button.dataset.view === state.stage).textContent;
  const group = at("record-tabs");
  const restoreFocus = group.contains(document.activeElement);
  group.replaceChildren(...tabs.map(([label], index) => {
    const button = tag("button", label);
    button.type = "button";
    button.dataset.tab = index;
    button.setAttribute("aria-pressed", String(index === state.tab));
    return button;
  }));
  if (restoreFocus) group.querySelector("[aria-pressed=true]")?.focus();
  drawTable(tabs[state.tab]);
  if (state.stage === "supplement") {
    const manifest = state.supplement?.manifest;
    at("supp-meta").textContent = manifest
      ? `补裁单 ${manifest.id.slice(0, 12)} · 补裁 ${manifest.piece_count} 片 · 待补 ${manifest.shortfall} 片 · 补后余 ${manifest.totals.remaining} mm`
      : state.supplements.length ? "从补单登记选择批次，或登记新补单。" : "登记短单后生成追加补裁单。";
  }
}

function draw() {
  heading();
  rollPanel();
  recordsPanel();
  archiveControls();
}

function setRun(document) {
  state.document = document;
  if (document.manifest) {
    sample.value = document.manifest.sample;
    kerf.value = document.manifest.kerf_mm;
  }
}

async function preview() {
  const query = new URLSearchParams({sample: sample.value, kerf_mm: kerf.value});
  setRun(await fetchData(`/api/preview?${query}`));
  state.supplements = [];
  state.supplement = null;
}

async function openRun(id) {
  setRun(await fetchData(`/api/runs/${id}`));
  state.supplements = (await fetchData(`/api/runs/${id}/supplements`)).supplements;
  state.supplement = null;
  if (state.stage === "supplement" && state.supplements.length) {
    state.supplement = await fetchData(`/api/supplements/${state.supplements[0].id}`);
  }
}

async function recent() {
  state.history = (await fetchData("/api/runs")).runs;
}

async function action(work) {
  if (state.busy) return;
  state.busy = true;
  notice();
  heading();
  archiveControls();
  try {
    await work();
    at("sync-state").textContent = "服务在线";
  } catch (error) {
    notice(`${error.message}，请核对后重试。`, true);
    at("sync-state").textContent = "作业失败";
  } finally {
    state.busy = false;
    draw();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const outcome = await fetchData("/api/runs", "POST", {sample: sample.value, kerf_mm: Number(kerf.value)});
    await openRun(outcome.manifest.id);
    await recent();
    notice(outcome.reused ? "沿用原归档" : "卷宗已归档");
  });
});
suppForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const run = state.document?.manifest?.id;
  if (!run) {
    notice("请先归档批次，再登记补单。", true);
    return;
  }
  action(async () => {
    const outcome = await fetchData(`/api/runs/${run}/supplements`, "POST", Object.fromEntries(new FormData(suppForm)));
    state.supplement = await fetchData(`/api/supplements/${outcome.manifest.id}`);
    state.supplements = (await fetchData(`/api/runs/${run}/supplements`)).supplements;
    notice(outcome.reused ? "沿用原补裁单" : "补裁单已归档");
  });
});
at("supp-rerun").addEventListener("click", () => action(async () => {
  await fetchData(`/api/supplements/${state.supplement.manifest.id}/rerun`, "POST");
  notice("重算与归档一致");
}));
sample.addEventListener("change", () => {
  if (kerf.reportValidity()) action(preview);
  else archiveControls();
});
kerf.addEventListener("input", archiveControls);
at("rerun").addEventListener("click", () => action(async () => {
  const id = state.document.manifest.id;
  await fetchData(`/api/runs/${id}/rerun`, "POST");
  notice("重算与归档一致");
}));
at("print").addEventListener("click", () => window.print());
for (const button of navigation) button.addEventListener("click", () => {
  state.stage = button.dataset.view;
  state.tab = 0;
  navigation.forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  if (state.stage === "supplement" && !state.supplement && state.supplements.length) {
    action(async () => {
      state.supplement = await fetchData(`/api/supplements/${state.supplements[0].id}`);
    });
  } else {
    recordsPanel();
  }
});
at("record-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab]");
  if (button) {
    state.tab = Number(button.dataset.tab);
    recordsPanel();
  }
});
function chooseRoll(roll) {
  state.roll = roll;
  rollPanel();
  if (state.stage === "zones") recordsPanel();
  archiveControls();
}
at("roll-picker").addEventListener("click", (event) => {
  const button = event.target.closest("[data-roll]");
  if (button) chooseRoll(button.dataset.roll);
});
at("record-table").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button?.dataset.roll) chooseRoll(button.dataset.roll);
  if (button?.dataset.open) action(async () => openRun(button.dataset.open));
  if (button?.dataset.supp) action(async () => {
    state.supplement = await fetchData(`/api/supplements/${button.dataset.supp}`);
  });
});

action(async () => {
  await recent();
  if (state.history.length) await openRun(state.history[0].id);
  else await preview();
});
