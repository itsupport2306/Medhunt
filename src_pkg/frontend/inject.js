



(() => {
  "use strict";
  if (window.__medhuntResumeHookLoaded) return;
  window.__medhuntResumeHookLoaded = true;

  const RESUME_TYPE = /(pdf|officedocument|msword|wordprocessing|octet-stream)/i;

  function postBytes(buffer, contentType, tentative = false) {
    try {
      const bytes = new Uint8Array(buffer);
      let binary = "";
      const chunkSize = 0x8000;
      for (let index = 0; index < bytes.length; index += chunkSize) {
        binary += String.fromCharCode.apply(
          null,
          bytes.subarray(index, index + chunkSize),
        );
      }
      window.postMessage({
        __medhuntResume: true,
        base64: btoa(binary),
        contentType: contentType || "",
        tentative: Boolean(tentative),
      }, "*");
    } catch {

    }
  }

  const originalCreateObjectURL = URL.createObjectURL.bind(URL);
  URL.createObjectURL = function medhuntCreateObjectURL(value) {
    try {
      if (value instanceof Blob) {
        const definite = RESUME_TYPE.test(value.type || "");
        value.arrayBuffer().then((buffer) => postBytes(buffer, value.type, !definite));
      }
    } catch {}
    return originalCreateObjectURL(value);
  };

  const originalFetch = window.fetch;
  if (originalFetch) {
    window.fetch = function medhuntFetch() {
      return originalFetch.apply(this, arguments).then((response) => {
        try {
          const contentType = response.headers.get("content-type") || "";
          if (RESUME_TYPE.test(contentType)) {
            response.clone().arrayBuffer().then((buffer) => postBytes(buffer, contentType));
          }
        } catch {}
        return response;
      });
    };
  }

  const OriginalXMLHttpRequest = window.XMLHttpRequest;
  if (OriginalXMLHttpRequest) {
    const WrappedXMLHttpRequest = function medhuntXMLHttpRequest() {
      const request = new OriginalXMLHttpRequest();
      request.addEventListener("load", function captureXHRResume() {
        try {
          const contentType = request.getResponseHeader?.("content-type") || "";
          if (!RESUME_TYPE.test(contentType)) return;
          if (request.response instanceof ArrayBuffer) {
            postBytes(request.response, contentType);
          } else if (request.response instanceof Blob) {
            request.response.arrayBuffer().then((buffer) => postBytes(buffer, contentType));
          }
        } catch {}
      });
      return request;
    };
    WrappedXMLHttpRequest.prototype = OriginalXMLHttpRequest.prototype;
    window.XMLHttpRequest = WrappedXMLHttpRequest;
  }
})();
