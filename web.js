"use strict";

const selected = document.querySelector(".focused");
if (selected) selected.scrollIntoView({ block: "center" });

const stage = document.querySelector(".graph-stage");
if (stage) {
  const windowElement = stage.parentElement;
  const canvas = stage.querySelector("canvas");
  const graph = canvas.getContext("2d");
  const width = Number(stage.dataset.width);
  const height = Number(stage.dataset.height);
  const nodes = Array.from(stage.querySelectorAll(".graph-node"));
  const byKey = new Map(nodes.map(node => [node.dataset.key, node]));
  const edges = Array.from(stage.querySelectorAll(".graph-edges i"));
  const zoom = document.querySelector("#graph-zoom");

  function draw() {
    const scale = Number(zoom.value) / 100;
    const density = window.devicePixelRatio || 1;
    const viewWidth = Math.ceil(width * scale);
    const viewHeight = Math.ceil(height * scale);
    stage.style.width = viewWidth + "px";
    stage.style.height = viewHeight + "px";
    canvas.style.width = viewWidth + "px";
    canvas.style.height = viewHeight + "px";
    canvas.width = viewWidth * density;
    canvas.height = viewHeight * density;
    graph.setTransform(density, 0, 0, density, 0, 0);
    for (const edge of edges) {
      const from = byKey.get(edge.dataset.from);
      const to = byKey.get(edge.dataset.to);
      const x1 = Number(from.dataset.x) * scale;
      const y1 = Number(from.dataset.y) * scale;
      const x2 = Number(to.dataset.x) * scale;
      const y2 = Number(to.dataset.y) * scale;
      graph.beginPath();
      graph.moveTo(x1, y1);
      graph.lineTo(x2, y2);
      graph.strokeStyle = edge.dataset.color;
      graph.lineWidth = 1.6;
      graph.stroke();
    }
    for (const node of nodes) {
      node.style.left = Number(node.dataset.x) * scale + "px";
      node.style.top = Number(node.dataset.y) * scale + "px";
    }
  }

  zoom.addEventListener("input", draw);
  for (const label of document.querySelectorAll(".legend-key")) {
    label.style.setProperty("--edge-color", label.dataset.color);
  }
  requestAnimationFrame(draw);
  windowElement.addEventListener("keydown", event => {
    if (event.key === "Home" && event.target === windowElement) windowElement.scrollTo(0, 0);
  });
  windowElement.tabIndex = 0;
}
