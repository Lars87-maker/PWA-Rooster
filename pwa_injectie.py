import streamlit as st
import streamlit.components.v1 as components

# --- PWA Injectie ---
components.html(
    """
    <link rel="manifest" href="/manifest.json">
    <script>
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/service-worker.js")
          .then(() => console.log("✅ Service Worker Registered"))
          .catch(err => console.error("❌ Service Worker failed:", err));
      }
    </script>
    """,
    height=0,
)
