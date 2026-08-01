// Shared top-nav for every Wren view. Like chat-dock.js, this is one file
// included as <script src="/static/nav.js"></script>; its contract is a
// page-supplied mount element (<nav id="wren-nav">) that it fills with the
// canonical view list. Styling lives in nav.css — this emits structure and
// class names only. Add a view here and every page's menu picks it up.
//
// The nav lived copy-pasted in each page's header and drifted: different
// pages listed different subsets of views, and /chat had no menu at all.
(() => {
  const VIEWS = [
    { href: "/", label: "chat" },
    { href: "/dashboard", label: "dashboard" },
    { href: "/memories", label: "memories" },
    { href: "/opportunities", label: "opportunities" },
    { href: "/starred", label: "starred" },
    { href: "/games", label: "games" },
    { href: "/map", label: "map" },
  ];

  const mount = document.getElementById("wren-nav");
  if (!mount) return;                                    // degrade, don't throw

  const here = window.location.pathname.replace(/\/+$/, "") || "/";
  for (const v of VIEWS) {
    const active = v.href === here;
    const node = document.createElement(active ? "span" : "a");
    node.className = active ? "active" : "";
    node.textContent = v.label;
    if (active) node.setAttribute("aria-current", "page");
    else node.href = v.href;
    mount.appendChild(node);
  }
})();
