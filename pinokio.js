const fs = require('fs')
const path = require('path')

function getVersion() {
  try {
    return fs.readFileSync(path.join(__dirname, 'VERSION'), 'utf8').trim()
  } catch { return '?' }
}

module.exports = {
  version: "5.0",
  title: "Lover Clinic - AI Voice System",
  description: "ระบบแปลงเสียง AI สำหรับ Lover Clinic — Voice Conversion, Thai TTS, Vocal Enhancer",
  icon: "icon.png",
  menu: async (kernel, info) => {
    let installed = info.exists("app/env")
    let running = {
      install: info.running("install.js"),
      start: info.running("start.js"),
      update: info.running("update.js"),
      reset: info.running("reset.js"),
      fix: info.running("fix.js"),
    }
    let ver = getVersion()

    if (running.fix) {
      return [{
        default: true,
        icon: "fa-solid fa-wrench",
        text: "Fixing...",
        href: "fix.js",
      }]
    } else if (running.install) {
      return [{
        default: true,
        icon: "fa-solid fa-plug",
        text: "Installing",
        href: "install.js",
      }]
    } else if (installed) {
      if (running.start) {
        let local = info.local("start.js")
        if (local && local.url) {
          return [{
            default: true,
            icon: "fa-solid fa-rocket",
            text: "Open Web UI",
            href: local.url,
          }, {
            icon: "fa-solid fa-terminal",
            text: "Terminal",
            href: "start.js",
          }]
        } else {
          return [{
            default: true,
            icon: "fa-solid fa-terminal",
            text: "Terminal",
            href: "start.js",
          }]
        }
      } else if (running.update) {
        return [{
          default: true,
          icon: "fa-solid fa-terminal",
          text: "Updating",
          href: "update.js",
        }]
      } else if (running.reset) {
        return [{
          default: true,
          icon: "fa-solid fa-terminal",
          text: "Resetting",
          href: "reset.js",
        }]
      } else {
        return [{
          default: true,
          icon: "fa-solid fa-power-off",
          text: "Start",
          href: "start.js",
        }, {
          icon: "fa-solid fa-plug",
          text: "Update",
          href: "update.js",
        }, {
          icon: "fa-solid fa-plug",
          text: "Install",
          href: "install.js",
        }, {
          icon: "fa-solid fa-wrench",
          text: "<div><strong>Fix</strong><div>ติดตั้ง package ที่ขาดโดยไม่ต้อง Reset</div></div>",
          href: "fix.js",
        }, {
          icon: "fa-regular fa-circle-xmark",
          text: `<div><strong>Reset</strong><div>Revert to pre-install state</div></div>`,
          href: "reset.js",
          confirm: "Are you sure you wish to reset the app?"
        }, {
          icon: "fa-solid fa-info-circle",
          text: `v${ver}`,
        }]
      }
    } else {
      return [{
        default: true,
        icon: "fa-solid fa-plug",
        text: "Install",
        href: "install.js",
      }]
    }
  }
}
