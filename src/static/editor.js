// Редактор плана посадок: перенос, удаление и добавление растений.
// Правки проверяются на сервере по маскам допустимых зон и сохраняются
// в отдельный слой DXF, исходный расчёт не перезаписывается.
(function () {
  "use strict";

  var folder = document.body.getAttribute("data-folder");
  var svg = null;
  var layer = null;
  var items = [];
  var selected = null;
  var mode = "move";
  var drag = null;
  var dirty = false;
  var nextId = 1;
  var pan = null;
  var box = null;       // текущий viewBox: [x, y, w, h]
  var box0 = null;      // исходный, для кнопки «весь план»

  function el(id) { return document.getElementById(id); }

  function status(text, bad) {
    var s = el("status");
    s.textContent = text;
    s.className = bad ? "st bad" : "st";
  }

  function svgPoint(evt) {
    var pt = svg.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    var m = layer.getScreenCTM();
    if (!m) { return { x: 0, y: 0 }; }
    var p = pt.matrixTransform(m.inverse());
    return { x: p.x, y: p.y };
  }

  function radius(it) {
    var r = (it.crown_m || (it.type === "дерево" ? 6 : 1)) / 2;
    return Math.max(r, it.type === "дерево" ? 1.0 : 0.3);
  }

  function draw() {
    while (layer.firstChild) { layer.removeChild(layer.firstChild); }
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (it.deleted) { continue; }
      var c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", it.x);
      c.setAttribute("cy", it.y);
      c.setAttribute("r", radius(it));
      var cls = "pt " + (it.type === "дерево" ? "tree" : "shrub");
      if (it.bad) { cls += " bad"; }
      if (it.moved || it.added) { cls += " edited"; }
      if (selected === it) { cls += " sel"; }
      c.setAttribute("class", cls);
      c.setAttribute("data-idx", i);
      layer.appendChild(c);
    }
    var trees = 0, shrubs = 0;
    for (var j = 0; j < items.length; j++) {
      if (items[j].deleted) { continue; }
      if (items[j].type === "дерево") { trees++; } else { shrubs++; }
    }
    el("counts").textContent = "деревьев " + trees + ", кустарников " + shrubs;
  }

  function showInfo(it) {
    var box = el("info");
    if (!it) { box.innerHTML = '<span class="hint">Выберите растение на схеме</span>'; return; }
    var html = "<b>" + (it.id || "новое") + "</b> — " + (it.species_name || "") +
      "<br><span class='hint'>" + it.type + ", X " + it.x.toFixed(2) +
      " / Y " + it.y.toFixed(2) + "</span>";
    if (it.bad) { html += "<div class='badtxt'>" + it.bad + "</div>"; }
    if (it.explanation && !it.moved) {
      html += "<div class='exp'>" + it.explanation + "</div>";
    }
    box.innerHTML = html;
  }

  function hitIndex(evt) {
    var t = evt.target;
    if (t && t.getAttribute && t.getAttribute("data-idx") !== null) {
      return parseInt(t.getAttribute("data-idx"), 10);
    }
    return -1;
  }

  function onDown(evt) {
    var idx = hitIndex(evt);
    var p = svgPoint(evt);
    if (mode === "add-tree" || mode === "add-shrub") {
      var isTree = mode === "add-tree";
      var tpl = null;
      for (var k = 0; k < items.length; k++) {
        if ((items[k].type === "дерево") === isTree) { tpl = items[k]; break; }
      }
      var it = {
        id: (isTree ? "T-N" : "S-N") + (nextId++),
        type: isTree ? "дерево" : "кустарник",
        species_name: tpl ? tpl.species_name : (isTree ? "дерево" : "кустарник"),
        crown_m: tpl ? tpl.crown_m : (isTree ? 6 : 1),
        x: p.x, y: p.y, added: true
      };
      items.push(it);
      selected = it;
      dirty = true;
      draw();
      showInfo(it);
      status("Растение добавлено. Не забудьте сохранить.");
      return;
    }
    if (idx < 0) {
      // пустое место: в режиме переноса — сдвиг схемы
      selected = null;
      draw();
      showInfo(null);
      if (mode === "move") {
        pan = { x: evt.clientX, y: evt.clientY, bx: box[0], by: box[1] };
        svg.style.cursor = "grabbing";
      }
      return;
    }
    selected = items[idx];
    if (mode === "delete") {
      selected.deleted = true;
      selected = null;
      dirty = true;
      draw();
      showInfo(null);
      status("Растение удалено. Не забудьте сохранить.");
      return;
    }
    drag = { it: selected, dx: selected.x - p.x, dy: selected.y - p.y };
    draw();
    showInfo(selected);
    evt.preventDefault();
  }

  function applyBox() {
    svg.setAttribute("viewBox", box.join(" "));
  }

  function onMove(evt) {
    if (pan) {
      var r = svg.getBoundingClientRect();
      box[0] = pan.bx - (evt.clientX - pan.x) / r.width * box[2];
      box[1] = pan.by - (evt.clientY - pan.y) / r.height * box[3];
      applyBox();
      return;
    }
    if (!drag) { return; }
    var p = svgPoint(evt);
    drag.it.x = p.x + drag.dx;
    drag.it.y = p.y + drag.dy;
    drag.it.moved = true;
    drag.it.bad = null;
    dirty = true;
    draw();
    showInfo(drag.it);
  }

  function onUp() {
    if (drag) { status("Перенесено. Не забудьте сохранить."); }
    drag = null;
    if (pan) { pan = null; svg.style.cursor = ""; }
  }

  // Колесо мыши — приближение к точке под курсором.
  function onWheel(evt) {
    evt.preventDefault();
    var r = svg.getBoundingClientRect();
    var fx = (evt.clientX - r.left) / r.width;
    var fy = (evt.clientY - r.top) / r.height;
    var k = evt.deltaY > 0 ? 1.18 : 0.85;
    var nw = box[2] * k, nh = box[3] * k;
    // не даём уйти в бесконечное приближение или отдаление
    if (nw < box0[2] / 400 || nw > box0[2] * 3) { return; }
    box[0] = box[0] + (box[2] - nw) * fx;
    box[1] = box[1] + (box[3] - nh) * fy;
    box[2] = nw;
    box[3] = nh;
    applyBox();
  }

  function zoomBy(k) {
    var cx = box[0] + box[2] / 2, cy = box[1] + box[3] / 2;
    box[2] *= k;
    box[3] *= k;
    box[0] = cx - box[2] / 2;
    box[1] = cy - box[3] / 2;
    applyBox();
  }

  function setMode(m) {
    mode = m;
    var btns = document.querySelectorAll("[data-mode]");
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute("data-mode") === m;
      btns[i].className = on ? "tool on" : "tool";
    }
  }

  function save() {
    status("Сохраняю и проверяю нормы ...");
    var payload = { items: items };
    fetch("/api/plan/" + encodeURIComponent(folder), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (!r.ok) { return r.text().then(function (t) { throw new Error(t); }); }
      return r.json();
    }).then(function (res) {
      var byId = {};
      for (var i = 0; i < res.violations.length; i++) {
        byId[res.violations[i].id] = res.violations[i].reason;
      }
      for (var j = 0; j < items.length; j++) {
        items[j].bad = byId[items[j].id] || null;
      }
      dirty = false;
      draw();
      showInfo(selected);
      var msg = "Сохранено: " + res.file + ". ";
      if (res.violations.length) {
        status(msg + "Вне допустимой зоны: " + res.violations.length +
               " (отмечены красным)", true);
      } else {
        status(msg + "Все посадки в допустимой зоне.");
      }
      var a = el("dl");
      a.href = res.url;
      a.style.display = "inline-block";
    }).catch(function (e) {
      status("Ошибка сохранения: " + e.message, true);
    });
  }

  function start() {
    fetch("/api/plan/" + encodeURIComponent(folder)).then(function (r) {
      if (!r.ok) { return r.text().then(function (t) { throw new Error(t); }); }
      return r.json();
    }).then(function (data) {
      el("canvas").innerHTML = data.svg;
      svg = el("canvas").querySelector("svg");
      if (!svg) { throw new Error("схема плана не найдена"); }
      var root = svg.querySelector("g");
      var old = svg.querySelectorAll("#lay_trees, #lay_shrubs");
      for (var i = 0; i < old.length; i++) { old[i].parentNode.removeChild(old[i]); }
      layer = document.createElementNS("http://www.w3.org/2000/svg", "g");
      layer.setAttribute("id", "edit_layer");
      root.appendChild(layer);
      var vb = (svg.getAttribute("viewBox") || "0 0 100 100").split(/[ ,]+/);
      box = [parseFloat(vb[0]), parseFloat(vb[1]),
             parseFloat(vb[2]), parseFloat(vb[3])];
      box0 = box.slice();
      items = data.items;
      draw();
      showInfo(null);
      svg.addEventListener("wheel", onWheel, { passive: false });
      svg.addEventListener("mousedown", onDown);
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
      status("Готово к правке: " + items.length + " растений");
    }).catch(function (e) {
      status("Не удалось загрузить план: " + e.message, true);
    });

    var btns = document.querySelectorAll("[data-mode]");
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function (ev) {
        setMode(ev.currentTarget.getAttribute("data-mode"));
      });
    }
    el("save").addEventListener("click", save);
    el("zin").addEventListener("click", function () { zoomBy(0.7); });
    el("zout").addEventListener("click", function () { zoomBy(1.4); });
    el("zfit").addEventListener("click", function () {
      box = box0.slice();
      applyBox();
    });
    window.addEventListener("beforeunload", function (e) {
      if (dirty) { e.preventDefault(); e.returnValue = ""; }
    });
    setMode("move");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
