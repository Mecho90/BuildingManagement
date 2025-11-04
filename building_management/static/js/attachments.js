;(function () {
  const wrapper = document.querySelector("[data-attachments-wrapper]")
  const attachmentsList = wrapper ? wrapper.querySelector("[data-attachments]") : null
  const emptyState = wrapper ? wrapper.querySelector("[data-attachments-empty]") : null
  const uploadRoot = document.querySelector("[data-attachment-upload]")

  if (!attachmentsList && !uploadRoot) {
    return
  }

  const viewerLabels = attachmentsList
    ? {
        zoomIn: attachmentsList.dataset.labelZoomIn || "Zoom in",
        zoomOut: attachmentsList.dataset.labelZoomOut || "Zoom out",
        close: attachmentsList.dataset.labelClose || "Close",
        reset: attachmentsList.dataset.labelReset || "Reset zoom",
        open: attachmentsList.dataset.labelOpen || "Open original",
        loading: attachmentsList.dataset.labelLoading || "Loading..."
      }
    : {}

  const tapHintLabel = attachmentsList ? attachmentsList.dataset.labelTapHint || "Tap to zoom" : "Tap to zoom"
  const downloadLabel = attachmentsList ? attachmentsList.dataset.labelDownload || "Download" : "Download"
  const zoomLabel = attachmentsList ? attachmentsList.dataset.labelZoom || "Zoom" : "Zoom"
  const previewLabel = attachmentsList ? attachmentsList.dataset.labelPreview || "Preview" : "Preview"
  const docLoadingLabel = attachmentsList ? attachmentsList.dataset.labelDocLoading || "Loading preview..." : "Loading preview..."
  const deleteLabel = attachmentsList ? attachmentsList.dataset.labelDelete || "Delete" : "Delete"
  const deleteNext = attachmentsList
    ? attachmentsList.dataset.nextUrl || `${window.location.pathname}${window.location.search}`
    : `${window.location.pathname}${window.location.search}`
  const uploadedTemplate =
    (attachmentsList && attachmentsList.dataset.labelUploaded) ||
    (uploadRoot && uploadRoot.dataset.labelUploadedTemplate) ||
    "Uploaded %(date)s"
  const fallbackEmptyLabel =
    (attachmentsList && attachmentsList.dataset.labelEmpty) ||
    (uploadRoot && uploadRoot.dataset.labelEmpty) ||
    "No attachments uploaded yet."
  const filePlaceholderLabel = attachmentsList ? attachmentsList.dataset.labelFile || "FILE" : "FILE"

  const canManage = attachmentsList ? attachmentsList.dataset.canManage === "1" : false
  const csrfToken = getCSRFToken()

  // Viewer state ------------------------------------------------------------
  let overlay
  let overlayImage
  let overlayTitle
  let overlayDownload
  let overlaySpinner
  let lastActiveTrigger = null
  let initialBodyOverflow = null
  let docOverlay
  let docFrame
  let docTitle
  let docDownload
  let docSpinner
  let lastActiveDocTrigger = null

  const pointers = new Map()
  const SCALE_STEP = 0.25
  const MIN_SCALE = 1
  const MAX_SCALE = 5
  let scale = 1
  let panX = 0
  let panY = 0
  let isDragging = false
  let dragStartX = 0
  let dragStartY = 0
  let initialPinchDistance = 0
  let pinchStartScale = 1

  if (attachmentsList) {
    attachmentsList.addEventListener("click", onAttachmentsClick)
  }

  if (uploadRoot && attachmentsList) {
    initUploader()
  }

  // -------------------------------------------------------------------------
  // Attachment actions
  // -------------------------------------------------------------------------

  function onAttachmentsClick (event) {
    const target = event.target
    if (!(target instanceof HTMLElement)) {
      return
    }

    const viewerTrigger = target.closest("[data-attachment-viewer]")
    if (viewerTrigger) {
      event.preventDefault()
      openOverlay(viewerTrigger)
      return
    }

    const previewTrigger = target.closest("[data-attachment-preview]")
    if (previewTrigger) {
      event.preventDefault()
      if (previewTrigger.dataset.external === "1") {
        window.open(previewTrigger.dataset.src, "_blank")
      } else {
        openDocOverlay(previewTrigger)
      }
      return
    }
  }

  function updateEmptyState () {
    if (!attachmentsList || !emptyState) {
      return
    }
    const hasItems = !!attachmentsList.querySelector(".attachments-grid__item")
    if (hasItems) {
      emptyState.hidden = true
      emptyState.textContent = fallbackEmptyLabel
    } else {
      emptyState.hidden = false
      emptyState.textContent = fallbackEmptyLabel
    }
  }

  function addAttachmentFromMeta (meta) {
    if (!attachmentsList) {
      return
    }
    const element = renderAttachment(meta)
    if (!element) {
      return
    }
    const existing = attachmentsList.querySelector(
      '[data-attachment-id="' + escapeSelector(String(meta.id)) + '"]'
    )
    if (existing) {
      existing.remove()
    }
    const first = attachmentsList.querySelector(".attachments-grid__item")
    if (first) {
      attachmentsList.insertBefore(element, first)
    } else {
      attachmentsList.appendChild(element)
    }
    updateEmptyState()
  }

  function renderAttachment (meta) {
    if (!meta || typeof meta !== "object") {
      return null
    }

    const li = document.createElement("li")
    li.className = "attachments-grid__item"
    li.dataset.attachmentId = String(meta.id)

    const article = document.createElement("article")
    article.className = "attachment-card attachment-card--" + (meta.category || "file")
    li.appendChild(article)

    const media = document.createElement("div")
    media.className = "attachment-card__media"
    article.appendChild(media)

    if (meta.is_image && meta.url) {
      const button = document.createElement("button")
      button.type = "button"
      button.className = "attachment-card__preview"
      button.dataset.attachmentViewer = "1"
      button.dataset.src = meta.url
      button.dataset.name = meta.name || ""
      button.dataset.type = meta.content_type || ""

      const img = document.createElement("img")
      img.src = meta.url
      img.alt = meta.name || ""
      img.loading = "lazy"
      button.appendChild(img)

      const hint = document.createElement("span")
      hint.className = "attachment-card__preview-hint"
      hint.textContent = tapHintLabel
      button.appendChild(hint)

      media.appendChild(button)
    } else {
      const placeholder = document.createElement("div")
      placeholder.className = "attachment-card__placeholder"
      placeholder.setAttribute("aria-hidden", "true")
      const label = document.createElement("span")
      label.className = "attachment-card__placeholder-text"
      const extension = (meta.extension || "").toString().trim()
      label.textContent = extension ? extension.toUpperCase() : filePlaceholderLabel
      placeholder.appendChild(label)
      media.appendChild(placeholder)
    }

    const body = document.createElement("div")
    body.className = "attachment-card__body"
    article.appendChild(body)

    const title = document.createElement("h3")
    title.className = "attachment-card__title"
    title.textContent = meta.name || "Attachment"
    body.appendChild(title)

    const metaRow = document.createElement("div")
    metaRow.className = "attachment-card__meta"
    if (meta.content_type) {
      const typeBadge = document.createElement("span")
      typeBadge.textContent = meta.content_type
      metaRow.appendChild(typeBadge)
    }
    const sizeDisplay = meta.size_display || meta.size_label
    if (sizeDisplay) {
      const sizeBadge = document.createElement("span")
      sizeBadge.textContent = sizeDisplay
      metaRow.appendChild(sizeBadge)
    }
    if (metaRow.children.length) {
      body.appendChild(metaRow)
    }

    if (meta.url) {
      const actions = document.createElement("div")
      actions.className = "attachment-card__actions"

      const downloadLink = document.createElement("a")
      downloadLink.className = "attachment-card__action"
      downloadLink.href = meta.url
      downloadLink.target = "_blank"
      downloadLink.rel = "noopener noreferrer"
      downloadLink.textContent = downloadLabel
      actions.appendChild(downloadLink)

      if (meta.is_image) {
        const zoomButton = document.createElement("button")
        zoomButton.type = "button"
        zoomButton.className = "attachment-card__action attachment-card__action--secondary"
        zoomButton.dataset.attachmentViewer = "1"
        zoomButton.dataset.src = meta.url
        zoomButton.dataset.name = meta.name || ""
        zoomButton.dataset.type = meta.content_type || ""
        zoomButton.textContent = zoomLabel
        actions.appendChild(zoomButton)
      }

      if (canManage && meta.delete_url) {
        const deleteLink = document.createElement("a")
        deleteLink.className = "attachment-card__action attachment-card__action--danger"
        deleteLink.href = decorateDeleteUrl(meta.delete_url)
        deleteLink.textContent = deleteLabel
        if (meta.delete_confirm) {
          deleteLink.title = stripTags(meta.delete_confirm)
        }
        actions.appendChild(deleteLink)
      }

      body.appendChild(actions)
    }

    const time = document.createElement("time")
    time.className = "attachment-card__timestamp"
    const createdDisplay = meta.created_display || formatDate(meta.created_at)
    time.dateTime = meta.created_at || meta.created_iso || formatISO(meta.created_at)
    time.textContent = formatUploadedLabel(createdDisplay)
    body.appendChild(time)

    return li
  }

  function formatUploadedLabel (dateValue) {
    return uploadedTemplate.replace("%(date)s", dateValue || "")
  }

  function formatDate (value) {
    if (!value) {
      return ""
    }
    try {
      const date = new Date(value)
      if (!Number.isNaN(date.getTime())) {
        const year = date.getFullYear()
        const month = String(date.getMonth() + 1).padStart(2, "0")
        const day = String(date.getDate()).padStart(2, "0")
        const hours = String(date.getHours()).padStart(2, "0")
        const minutes = String(date.getMinutes()).padStart(2, "0")
        return `${year}-${month}-${day} ${hours}:${minutes}`
      }
    } catch (error) {
      /* noop */
    }
    return String(value)
  }

  function formatISO (value) {
    try {
      const date = new Date(value)
      if (!Number.isNaN(date.getTime())) {
        return date.toISOString()
      }
    } catch (error) {
      /* noop */
    }
    return ""
  }

  function decorateDeleteUrl (url) {
    if (!url) {
      return url
    }
    try {
      const parsed = new URL(url, window.location.origin)
      if (deleteNext && !parsed.searchParams.has("next")) {
        parsed.searchParams.set("next", deleteNext)
      }
      return parsed.pathname + parsed.search + parsed.hash
    } catch (error) {
      /* noop */
    }
    if (url.includes("next=") || !deleteNext) {
      return url
    }
    const glue = url.includes("?") ? "&" : "?"
    return `${url}${glue}next=${encodeURIComponent(deleteNext)}`
  }

  // -------------------------------------------------------------------------
  // Viewer (lightbox)
  // -------------------------------------------------------------------------

  function ensureOverlay () {
    if (overlay) {
      return overlay
    }

    overlay = document.createElement("div")
    overlay.className = "attachment-lightbox"
    overlay.setAttribute("hidden", "hidden")

    overlay.innerHTML = `
      <div class="attachment-lightbox__backdrop" data-action="close"></div>
      <div class="attachment-lightbox__inner" role="dialog" aria-modal="true">
        <header class="attachment-lightbox__toolbar">
          <span class="attachment-lightbox__title"></span>
          <div class="attachment-lightbox__buttons">
            <button type="button" class="attachment-lightbox__button" data-action="zoom-out" aria-label="${viewerLabels.zoomOut}" title="${viewerLabels.zoomOut}">−</button>
            <button type="button" class="attachment-lightbox__button" data-action="zoom-in" aria-label="${viewerLabels.zoomIn}" title="${viewerLabels.zoomIn}">+</button>
            <button type="button" class="attachment-lightbox__button" data-action="reset" aria-label="${viewerLabels.reset}" title="${viewerLabels.reset}">⤾</button>
            <a class="attachment-lightbox__link" data-action="open" aria-label="${viewerLabels.open}" title="${viewerLabels.open}" target="_blank" rel="noopener noreferrer">↗</a>
            <button type="button" class="attachment-lightbox__button" data-action="close" aria-label="${viewerLabels.close}" title="${viewerLabels.close}">×</button>
          </div>
        </header>
        <div class="attachment-lightbox__stage">
          <div class="attachment-lightbox__spinner" aria-hidden="true"></div>
          <img class="attachment-lightbox__image" alt=""/>
        </div>
      </div>
    `

    overlayImage = overlay.querySelector(".attachment-lightbox__image")
    overlayTitle = overlay.querySelector(".attachment-lightbox__title")
    overlayDownload = overlay.querySelector('[data-action="open"]')
    overlaySpinner = overlay.querySelector(".attachment-lightbox__spinner")
    if (overlaySpinner) {
      overlaySpinner.setAttribute("role", "status")
      overlaySpinner.setAttribute("aria-label", viewerLabels.loading)
    }

    overlay.addEventListener("click", onOverlayClick)
    overlayImage.addEventListener("load", function () {
      if (overlaySpinner) {
        overlaySpinner.setAttribute("hidden", "hidden")
      }
    })
    overlayImage.addEventListener("error", function () {
      if (overlaySpinner) {
        overlaySpinner.setAttribute("hidden", "hidden")
      }
    })

    overlayImage.addEventListener("pointerdown", onPointerDown)
    overlayImage.addEventListener("pointermove", onPointerMove)
    overlayImage.addEventListener("pointerup", onPointerUp)
    overlayImage.addEventListener("pointercancel", onPointerUp)
    overlayImage.addEventListener("wheel", onWheel, { passive: false })

    document.body.appendChild(overlay)
    return overlay
  }

  function onOverlayClick (event) {
    const target = event.target
    if (!(target instanceof HTMLElement)) {
      return
    }
    const action = target.dataset.action
    switch (action) {
      case "close":
        closeOverlay()
        break
      case "zoom-in":
        stepZoom(SCALE_STEP)
        break
      case "zoom-out":
        stepZoom(-SCALE_STEP)
        break
      case "reset":
        resetTransform()
        break
      default:
        break
    }
  }

  function onKeyDown (event) {
    if (!overlay || overlay.hasAttribute("hidden")) {
      return
    }
    switch (event.key) {
      case "Escape":
        event.preventDefault()
        closeOverlay()
        break
      case "+":
      case "=":
        event.preventDefault()
        stepZoom(SCALE_STEP)
        break
      case "-":
      case "_":
        event.preventDefault()
        stepZoom(-SCALE_STEP)
        break
      case "0":
        event.preventDefault()
        resetTransform()
        break
      default:
        break
    }
  }

  function stepZoom (delta) {
    setScale(scale + delta, { keepPan: false })
  }

  function setScale (next, options) {
    const keepPan = options && options.keepPan
    const clamped = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next))
    scale = parseFloat(clamped.toFixed(2))
    if (!keepPan && scale === MIN_SCALE) {
      panX = 0
      panY = 0
    }
    updateTransform()
  }

  function resetTransform () {
    scale = 1
    panX = 0
    panY = 0
    updateTransform()
  }

  function updateTransform () {
    if (!overlayImage) {
      return
    }
    overlayImage.style.transform = `translate3d(${panX}px, ${panY}px, 0) scale(${scale})`
    if (overlay) {
      if (scale > 1.01) {
        overlay.classList.add("attachment-lightbox--zoomed")
      } else {
        overlay.classList.remove("attachment-lightbox--zoomed")
      }
    }
  }

  function onWheel (event) {
    event.preventDefault()
    const direction = event.deltaY < 0 ? 1 : -1
    const prevScale = scale
    stepZoom(direction * SCALE_STEP)
    if (scale > prevScale && overlayImage) {
      const rect = overlayImage.getBoundingClientRect()
      const offsetX = event.clientX - (rect.left + rect.width / 2)
      const offsetY = event.clientY - (rect.top + rect.height / 2)
      panX += offsetX * 0.05
      panY += offsetY * 0.05
      updateTransform()
    }
  }

  function onPointerDown (event) {
    if (!overlayImage) {
      return
    }
    event.preventDefault()
    overlayImage.setPointerCapture(event.pointerId)
    pointers.set(event.pointerId, event)

    if (pointers.size === 1) {
      isDragging = scale > 1
      dragStartX = event.clientX - panX
      dragStartY = event.clientY - panY
    } else if (pointers.size === 2) {
      const values = Array.from(pointers.values())
      initialPinchDistance = distanceBetween(values[0], values[1])
      pinchStartScale = scale
    }
    if (overlay) {
      overlay.classList.add("attachment-lightbox--dragging")
    }
  }

  function onPointerMove (event) {
    if (!pointers.has(event.pointerId)) {
      return
    }
    pointers.set(event.pointerId, event)

    if (pointers.size === 1 && isDragging) {
      const pointer = pointers.values().next().value
      panX = pointer.clientX - dragStartX
      panY = pointer.clientY - dragStartY
      updateTransform()
    } else if (pointers.size === 2) {
      const values = Array.from(pointers.values())
      const currentDistance = distanceBetween(values[0], values[1])
      if (initialPinchDistance > 0) {
        const ratio = currentDistance / initialPinchDistance
        setScale(pinchStartScale * ratio, { keepPan: true })
      }
    }
  }

  function onPointerUp (event) {
    if (!overlayImage) {
      return
    }
    if (overlay) {
      overlay.classList.remove("attachment-lightbox--dragging")
    }
    pointers.delete(event.pointerId)
    overlayImage.releasePointerCapture(event.pointerId)
    if (pointers.size < 2) {
      initialPinchDistance = 0
      pinchStartScale = scale
    }
    if (pointers.size === 0) {
      isDragging = false
    }
  }

  function distanceBetween (a, b) {
    return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY)
  }

  function openOverlay (trigger) {
    if (!trigger) {
      return
    }
    const src = trigger.dataset.src
    if (!src) {
      return
    }
    ensureOverlay()
    lastActiveTrigger = trigger
    if (overlaySpinner) {
      overlaySpinner.removeAttribute("hidden")
    }
    if (overlayImage) {
      overlayImage.setAttribute("src", src)
      overlayImage.setAttribute("alt", trigger.dataset.name || "")
    }
    if (overlayTitle) {
      overlayTitle.textContent = trigger.dataset.name || ""
    }
    if (overlayDownload) {
      overlayDownload.setAttribute("href", src)
    }

    resetTransform()

    if (overlay) {
      overlay.removeAttribute("hidden")
      overlay.setAttribute("data-active", "true")
    }

    initialBodyOverflow = document.body.style.overflow
    document.body.style.overflow = "hidden"
    document.addEventListener("keydown", onKeyDown)

    const closeButton = overlay ? overlay.querySelector('[data-action="close"]') : null
    if (closeButton instanceof HTMLElement) {
      closeButton.focus()
    }
  }

  function closeOverlay () {
    if (!overlay) {
      return
    }
    overlay.setAttribute("hidden", "hidden")
    overlay.removeAttribute("data-active")
    document.body.style.overflow = initialBodyOverflow || ""
    document.removeEventListener("keydown", onKeyDown)
    if (overlayImage) {
      overlayImage.setAttribute("src", "")
    }
    pointers.clear()
    isDragging = false
    initialPinchDistance = 0
    pinchStartScale = 1
    if (lastActiveTrigger instanceof HTMLElement) {
      lastActiveTrigger.focus()
    }
  }

  // -------------------------------------------------------------------------
  // Document preview modal
  // -------------------------------------------------------------------------

  function ensureDocOverlay () {
    if (docOverlay) {
      return docOverlay
    }

    docOverlay = document.createElement("div")
    docOverlay.className = "attachment-doc-modal"
    docOverlay.setAttribute("hidden", "hidden")

    docOverlay.innerHTML = `
      <div class="attachment-doc-modal__backdrop" data-doc-action="close"></div>
      <div class="attachment-doc-modal__inner" role="dialog" aria-modal="true">
        <header class="attachment-doc-modal__toolbar">
          <span class="attachment-doc-modal__title"></span>
          <div class="attachment-doc-modal__buttons">
            <a class="attachment-doc-modal__button" data-doc-action="open" target="_blank" rel="noopener noreferrer">↗</a>
            <button type="button" class="attachment-doc-modal__button" data-doc-action="close" aria-label="${viewerLabels.close || "Close"}" title="${viewerLabels.close || "Close"}">×</button>
          </div>
        </header>
        <div class="attachment-doc-modal__stage">
          <div class="attachment-doc-modal__spinner" aria-hidden="true"></div>
          <iframe class="attachment-doc-modal__frame" title="" referrerpolicy="no-referrer"></iframe>
        </div>
      </div>
    `

    docFrame = docOverlay.querySelector(".attachment-doc-modal__frame")
    docTitle = docOverlay.querySelector(".attachment-doc-modal__title")
    docDownload = docOverlay.querySelector('[data-doc-action="open"]')
    docSpinner = docOverlay.querySelector(".attachment-doc-modal__spinner")
    if (docSpinner) {
      docSpinner.setAttribute("role", "status")
      docSpinner.setAttribute("aria-label", docLoadingLabel)
    }

    docOverlay.addEventListener("click", onDocOverlayClick)
    if (docFrame) {
      docFrame.addEventListener("load", function () {
        if (docSpinner) {
          docSpinner.setAttribute("hidden", "hidden")
        }
      })
    }

    document.body.appendChild(docOverlay)
    return docOverlay
  }

  function onDocOverlayClick (event) {
    const target = event.target
    if (!(target instanceof HTMLElement)) {
      return
    }
    const action = target.dataset.docAction
    switch (action) {
      case "close":
        closeDocOverlay()
        break
      case "open":
        // handled via link
        break
    }
  }

  function onDocKeyDown (event) {
    if (!docOverlay || docOverlay.hasAttribute("hidden")) {
      return
    }
    if (event.key === "Escape") {
      event.preventDefault()
      closeDocOverlay()
    }
  }

  function openDocOverlay (trigger) {
    const src = trigger.dataset.src
    if (!src) {
      return
    }
    ensureDocOverlay()
    lastActiveDocTrigger = trigger
    if (docSpinner) {
      docSpinner.removeAttribute("hidden")
    }
    if (docFrame) {
      docFrame.setAttribute("src", src)
      docFrame.setAttribute("title", trigger.dataset.name || previewLabel)
    }
    if (docTitle) {
      docTitle.textContent = trigger.dataset.name || previewLabel
    }
    if (docDownload) {
      docDownload.setAttribute("href", src)
      docDownload.setAttribute("aria-label", (viewerLabels.open || "Open original") + " " + (trigger.dataset.name || ""))
      docDownload.setAttribute("title", viewerLabels.open || "Open original")
    }

    docOverlay.removeAttribute("hidden")
    initialBodyOverflow = document.body.style.overflow
    document.body.style.overflow = "hidden"
    document.addEventListener("keydown", onDocKeyDown)
    const closeButton = docOverlay.querySelector('[data-doc-action="close"]')
    if (closeButton instanceof HTMLElement) {
      closeButton.focus()
    }
  }

  function closeDocOverlay () {
    if (!docOverlay) {
      return
    }
    docOverlay.setAttribute("hidden", "hidden")
    document.body.style.overflow = initialBodyOverflow || ""
    document.removeEventListener("keydown", onDocKeyDown)
    if (docFrame) {
      docFrame.setAttribute("src", "about:blank")
    }
    if (docSpinner) {
      docSpinner.removeAttribute("hidden")
    }
    if (lastActiveDocTrigger instanceof HTMLElement) {
      lastActiveDocTrigger.focus()
    }
  }

  // -------------------------------------------------------------------------
  // Upload helper
  // -------------------------------------------------------------------------

  function initUploader () {
    const uploadEndpoint = uploadRoot.dataset.uploadEndpoint
    if (!uploadEndpoint) {
      return
    }

    const input = uploadRoot.querySelector("[data-attachment-upload-input]")
    const trigger = uploadRoot.querySelector("[data-attachment-upload-trigger]")
    const queue = uploadRoot.querySelector("[data-attachment-upload-queue]")
    const uploadLabels = {
      browse: uploadRoot.dataset.labelBrowse || "Upload files",
      uploading: uploadRoot.dataset.labelUploading || "Uploading…",
      uploaded: uploadRoot.dataset.labelUploaded || "Uploaded",
      failed: uploadRoot.dataset.labelFailed || "Upload failed"
    }

    if (trigger) {
      trigger.addEventListener("click", function () {
        if (input) {
          input.click()
        }
      })
    }

    if (input) {
      input.addEventListener("change", function () {
        if (!input.files || !input.files.length) {
          return
        }
        const files = Array.from(input.files)
        handleFiles(files)
        input.value = ""
      })
    }

    function handleFiles (files) {
      files.forEach(function (file) {
        uploadFile(file)
      })
    }

    function uploadFile (file) {
      const queueItem = createQueueItem(file.name, uploadLabels.uploading)
      if (queue) {
        queue.hidden = false
        queue.appendChild(queueItem.root)
      }

      const xhr = new XMLHttpRequest()
      xhr.open("POST", uploadEndpoint)
      xhr.responseType = "json"
      const headers = buildHeaders()
      Object.keys(headers).forEach(function (key) {
        xhr.setRequestHeader(key, headers[key])
      })

      xhr.upload.addEventListener("progress", function (event) {
        if (event.lengthComputable) {
          const percent = Math.round((event.loaded / event.total) * 100)
          queueItem.progress.style.width = percent + "%"
        }
      })

      xhr.addEventListener("load", function () {
        const status = xhr.status
        const response = xhr.response || safeParse(xhr.responseText)
        if (status >= 200 && status < 300) {
          queueItem.root.classList.remove("attachment-upload__item--error")
          queueItem.root.classList.add("attachment-upload__item--complete")
          queueItem.status.textContent = uploadLabels.uploaded
          queueItem.progress.style.width = "100%"
          const attachments = (response && response.attachments) || []
          attachments.forEach(addAttachmentFromMeta)
          scheduleQueueRemoval(queueItem.root)
        } else {
          const message =
            (response && response.error) ||
            (response && response.errors && response.errors[0] && response.errors[0].errors
              ? response.errors[0].errors.join(", ")
              : null) ||
            uploadLabels.failed
          queueItem.root.classList.add("attachment-upload__item--error")
          queueItem.status.textContent = message
          queueItem.progress.style.width = "100%"
        }
      })

      xhr.addEventListener("error", function () {
        queueItem.root.classList.add("attachment-upload__item--error")
        queueItem.status.textContent = uploadLabels.failed
        queueItem.progress.style.width = "100%"
      })

      const formData = new FormData()
      formData.append("files", file)
      xhr.send(formData)
    }

    function createQueueItem (name, statusText) {
      const root = document.createElement("div")
      root.className = "attachment-upload__item"

      const header = document.createElement("div")
      header.className = "attachment-upload__item-header"
      const title = document.createElement("span")
      title.className = "attachment-upload__item-name"
      title.textContent = name
      const status = document.createElement("span")
      status.className = "attachment-upload__item-status"
      status.textContent = statusText
      header.appendChild(title)
      header.appendChild(status)
      root.appendChild(header)

      const progressWrapper = document.createElement("div")
      progressWrapper.className = "attachment-upload__progress"
      const progressBar = document.createElement("div")
      progressBar.className = "attachment-upload__progress-bar"
      progressWrapper.appendChild(progressBar)
      root.appendChild(progressWrapper)

      return {
        root,
        status,
        progress: progressBar
      }
    }

    function scheduleQueueRemoval (element) {
      window.setTimeout(function () {
        element.remove()
        if (queue && !queue.children.length) {
          queue.hidden = true
        }
      }, 2500)
    }
  }

  // -------------------------------------------------------------------------
  // Utilities
  // -------------------------------------------------------------------------

  function safeParse (text) {
    if (!text) {
      return null
    }
    try {
      return JSON.parse(text)
    } catch (error) {
      return null
    }
  }

  function getCSRFToken () {
    const match = document.cookie.match(/csrftoken=([^;]+)/)
    return match ? decodeURIComponent(match[1]) : ""
  }

  function buildHeaders () {
    const headers = { "X-Requested-With": "XMLHttpRequest" }
    if (csrfToken) {
      headers["X-CSRFToken"] = csrfToken
    }
    return headers
  }

  function stripTags (value) {
    if (!value) {
      return ""
    }
    return String(value).replace(/<[^>]+>/g, "")
  }

  function escapeSelector (value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(value)
    }
    return String(value).replace(/["\\]/g, "\\$&")
  }

  updateEmptyState()
})()
