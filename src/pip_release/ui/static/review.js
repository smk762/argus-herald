const urlPattern = /https?:\/\/[^\s]+/g;

function weightedLength(text) {
  let total = 0;
  let pos = 0;
  for (const match of text.matchAll(urlPattern)) {
    total += match.index - pos;
    total += 23;
    pos = match.index + match[0].length;
  }
  return total + (text.length - pos);
}

function bindCounter(node) {
  const field = document.getElementById(node.dataset.for);
  const value = node.querySelector(".count-value");
  if (!field || !value) return;
  const limit = Number(node.dataset.limit || "0");
  const weighted = node.dataset.weighted === "1";
  const refresh = () => {
    const length = weighted ? weightedLength(field.value) : field.value.length;
    value.textContent = String(length);
    node.classList.toggle("over", limit > 0 && length > limit);
  };
  field.addEventListener("input", refresh);
  refresh();
}

document.querySelectorAll(".count").forEach(bindCounter);

function bindCopy(button) {
  const field = document.getElementById(button.dataset.copy);
  if (!field) return;
  const label = button.textContent;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(field.value);
      button.textContent = "Copied";
    } catch {
      field.select();
      button.textContent = "Select and copy";
    }
    setTimeout(() => { button.textContent = label; }, 1500);
  });
}

document.querySelectorAll("button.copy").forEach(bindCopy);
