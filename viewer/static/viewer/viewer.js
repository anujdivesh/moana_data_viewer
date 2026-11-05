(async function () {
  const token = JSON.parse(document.getElementById("cesium-token").textContent);
  if (token) {
    Cesium.Ion.defaultAccessToken = token;
  } else {
    console.warn("CESIUM_ION_TOKEN is not set; Ion terrain and imagery will not load.");
  }

  // Esri World Imagery: public tiles, no ArcGIS key needed (attribution required).
  const worldImagery = new Cesium.UrlTemplateImageryProvider({
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    maximumLevel: 19,
    credit: "Esri, Maxar, Earthstar Geographics, and the GIS User Community",
  });

  // No toolbar at the top right: one basemap, no search, home, view-mode or help buttons.
  const viewer = new Cesium.Viewer("cesiumContainer", {
    terrain: token ? Cesium.Terrain.fromWorldTerrain() : undefined,
    baseLayer: new Cesium.ImageryLayer(worldImagery),
    baseLayerPicker: false,
    sceneModePicker: false,
    navigationHelpButton: false,
    homeButton: false,
    geocoder: false,
    timeline: false,
    animation: false,
    // Clicking the map shows values from every layer instead (see "Values on click").
    infoBox: false,
    selectionIndicator: false,
  });

  // No distance fog or atmospheric haze over the globe, so data layers show true colours.
  viewer.scene.fog.enabled = true;
  viewer.scene.globe.showGroundAtmosphere = true;

  // Start over the Pacific.
  viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(178.0, -18.0, 8000000),
  });

  window.viewer = viewer;

  const COLORS = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6"];
  const COLORMAPS = ["viridis", "magma", "plasma", "cividis", "terrain", "gist_earth", "ocean", "rdylbu_r", "rdylgn", "spectral_r", "coolwarm", "blues", "greys"];
  const form = document.getElementById("open-form");
  const statusEl = document.getElementById("status");
  const listEl = document.getElementById("layer-list");
  const folderListEl = document.getElementById("folder-list");
  const browser = document.getElementById("browser");
  const browseList = document.getElementById("browse-list");
  const browsePath = document.getElementById("browse-path");
  const browseUp = document.getElementById("browse-up");
  const csrfToken = form.querySelector("[name=csrfmiddlewaretoken]").value;
  const KIND_LABELS = { vector: "vector", raster: "GeoTIFF", zarr: "Zarr", netcdf: "NetCDF", pointcloud: "point cloud", pmtiles: "PMTiles" };

  // Upload id -> Cesium.GeoJsonDataSource or Cesium.ImageryLayer, for layers that have been loaded.
  const layers = new Map();
  // Upload id -> {colormap, rescale} chosen in the panel, for single-band rasters.
  const rasterStyles = new Map();

  function colorFor(id) {
    return Cesium.Color.fromCssColorString(COLORS[id % COLORS.length]);
  }

  function setStatus(message, isError = false) {
    statusEl.textContent = message;
    statusEl.classList.toggle("error", isError);
    // Errors would go unseen in a collapsed panel.
    if (isError && message) setPanelOpen(true);
  }

  // The panel starts collapsed to its header; click the header to open or close it.
  const panel = document.getElementById("panel");
  const panelToggle = document.getElementById("panel-toggle");
  function setPanelOpen(open) {
    panel.classList.toggle("collapsed", !open);
    panelToggle.setAttribute("aria-expanded", String(open));
  }
  panelToggle.addEventListener("click", () => setPanelOpen(panel.classList.contains("collapsed")));

  // Terrain PMTiles replace the globe's elevation instead of drawing on it.
  function isTerrain(upload) {
    return upload.kind === "pmtiles" && upload.raster_info.tile_type === "terrain";
  }

  // Zarr stores and NetCDF files: variables on a lat/lon grid, with time/depth controls.
  function isGridded(upload) {
    return upload.kind === "zarr" || upload.kind === "netcdf";
  }

  // GeoTIFFs, gridded data and PMTiles are drawn as tiled imagery; vectors as GeoJSON.
  function isImagery(upload) {
    return upload.kind === "raster" || isGridded(upload) || (upload.kind === "pmtiles" && !isTerrain(upload));
  }

  // The terrain upload shown on the globe, if any; only one can be at a time.
  let terrainId = null;

  function useDefaultTerrain() {
    terrainId = null;
    if (token) {
      viewer.scene.setTerrain(Cesium.Terrain.fromWorldTerrain());
    } else {
      viewer.terrainProvider = new Cesium.EllipsoidTerrainProvider();
    }
  }

  async function createTerrainProvider(upload) {
    const provider = await Cesium.CesiumTerrainProvider.fromUrl(upload.terrain_url);
    // Survey terrain covers a small area; the globe still needs every root tile, so
    // give it flat ground wherever the archive has none instead of leaving a hole.
    const flat = new Cesium.EllipsoidTerrainProvider();
    const request = provider.requestTileGeometry.bind(provider);
    provider.requestTileGeometry = (x, y, level, req) =>
      level === 0 && !provider.getTileDataAvailable(x, y, level)
        ? flat.requestTileGeometry(x, y, level, req)
        : request(x, y, level, req);
    return provider;
  }

  async function showTerrain(upload) {
    let provider = layers.get(upload.id);
    if (!provider) {
      provider = await createTerrainProvider(upload);
      layers.set(upload.id, provider);
    }
    if (terrainId !== null && terrainId !== upload.id) {
      const other = listEl.querySelector(`li[data-id="${terrainId}"] input[type=checkbox]`);
      if (other) other.checked = false;
    }
    terrainId = upload.id;
    viewer.terrainProvider = provider;
  }

  function rasterRectangle(upload) {
    // Cesium rectangles may have west > east, which is how rasters crossing 180° arrive.
    return Cesium.Rectangle.fromDegrees(...upload.raster_info.bounds);
  }

  function createImageryLayer(upload, index) {
    const info = upload.raster_info;
    const params = new URLSearchParams();
    const style = rasterStyles.get(upload.id);
    if (style) {
      params.set("colormap", style.colormap);
      params.set("rescale", style.rescale.join(","));
      if (style.variable) params.set("variable", style.variable);
      const sel = Object.entries(style.sel || {}).map(([dim, index]) => `${dim}:${index}`).join(",");
      if (sel) params.set("sel", sel);
    }
    const query = params.toString();
    const [west, , east] = info.bounds;
    const provider = new Cesium.UrlTemplateImageryProvider({
      url: upload.tile_url + (query ? `?${query}` : ""),
      // Cesium skips imagery past 180° when the rectangle crosses the dateline (west > east),
      // so leave those layers unbounded; the server returns empty tiles outside the data.
      rectangle: west > east ? undefined : rasterRectangle(upload),
      maximumLevel: info.maxzoom,
      tileWidth: info.tile_size || 256,
      tileHeight: info.tile_size || 256,
      credit: upload.name,
    });
    return viewer.imageryLayers.addImageryProvider(provider, index);
  }

  // Lines draped on terrain render thin and broken at globe scale, and draped polygons
  // get no outline. Draw both as plain lines that stay visible through the terrain.
  function styleLines(dataSource, color) {
    const material = new Cesium.ColorMaterialProperty(color);
    const lineStyle = { width: 3, material, depthFailMaterial: material, clampToGround: false };
    for (const entity of [...dataSource.entities.values]) {
      if (entity.polyline) {
        Object.assign(entity.polyline, lineStyle);
      } else if (entity.polygon) {
        const hierarchy = entity.polygon.hierarchy.getValue(Cesium.JulianDate.now());
        const rings = [hierarchy.positions, ...hierarchy.holes.map((hole) => hole.positions)];
        for (const positions of rings) {
          dataSource.entities.add({
            parent: entity,
            polyline: { positions: [...positions, positions[0]], ...lineStyle },
          });
        }
      }
    }
  }

  async function showLayer(upload, zoom) {
    if (isTerrain(upload)) {
      await showTerrain(upload);
      if (zoom) viewer.camera.flyTo({ destination: rasterRectangle(upload) });
      return;
    }
    let layer = layers.get(upload.id);
    if (!layer) {
      if (upload.kind === "pointcloud") {
        layer = await Cesium.Cesium3DTileset.fromUrl(upload.tileset_url);
        layer.style = new Cesium.Cesium3DTileStyle({ pointSize: 2 });
        viewer.scene.primitives.add(layer);
      } else if (isImagery(upload)) {
        layer = createImageryLayer(upload);
      } else {
        const color = colorFor(upload.id);
        layer = await Cesium.GeoJsonDataSource.load(upload.geojson_url, {
          stroke: color,
          fill: color.withAlpha(0.4),
          markerColor: color,
          strokeWidth: 3,
          clampToGround: true,
        });
        styleLines(layer, color);
        layer.name = upload.name;
        await viewer.dataSources.add(layer);
      }
      layers.set(upload.id, layer);
    }
    layer.show = true;
    if (zoom) {
      if (upload.kind === "pointcloud") {
        await viewer.zoomTo(layer);
      } else if (isImagery(upload)) {
        viewer.camera.flyTo({ destination: rasterRectangle(upload) });
      } else {
        await viewer.flyTo(layer);
      }
    }
  }

  function hideLayer(id) {
    if (id === terrainId) {
      useDefaultTerrain();
      return;
    }
    const layer = layers.get(id);
    if (layer) layer.show = false;
  }

  function removeLayer(upload) {
    const layer = layers.get(upload.id);
    if (!layer) return;
    if (isTerrain(upload)) {
      if (upload.id === terrainId) useDefaultTerrain();
    } else if (upload.kind === "pointcloud") {
      viewer.scene.primitives.remove(layer);
    } else if (isImagery(upload)) {
      viewer.imageryLayers.remove(layer, true);
    } else {
      viewer.dataSources.remove(layer, true);
    }
    layers.delete(upload.id);
  }

  function restyleRaster(upload) {
    const old = layers.get(upload.id);
    if (!old) return;
    const index = viewer.imageryLayers.indexOf(old);
    const show = old.show;
    viewer.imageryLayers.remove(old, true);
    const layer = createImageryLayer(upload, index);
    layer.show = show;
    layers.set(upload.id, layer);
  }

  function describe(upload) {
    if (upload.kind === "pointcloud") {
      const info = upload.raster_info;
      return `${upload.name} (point cloud, ${info.points.toLocaleString()} points, ${upload.detected_crs})`;
    }
    if (isGridded(upload)) {
      const variables = Object.keys(upload.raster_info.variables).join(", ");
      return `${upload.name} (${KIND_LABELS[upload.kind]}: ${variables})`;
    }
    if (isTerrain(upload)) {
      const info = upload.raster_info;
      return `${upload.name} (PMTiles terrain, zoom ${info.minzoom}-${info.maxzoom})`;
    }
    if (upload.kind === "pmtiles") {
      const info = upload.raster_info;
      const layers = info.layers.length ? `: ${info.layers.join(", ")}` : "";
      return `${upload.name} (PMTiles, ${info.tile_type}, zoom ${info.minzoom}-${info.maxzoom}${layers})`;
    }
    if (upload.kind === "raster") {
      const info = upload.raster_info;
      const bands = info.rgb ? "RGB" : `${info.band_count} band${info.band_count > 1 ? "s" : ""}, ${info.dtype}`;
      return `${upload.name} (raster, ${bands}, ${upload.detected_crs})`;
    }
    return `${upload.name} (${upload.feature_count} features, from ${upload.detected_crs})`;
  }

  // Colormap picker and min/max inputs that edit style in place and call onChange.
  function colorControls(style, onChange) {
    const select = document.createElement("select");
    for (const name of new Set([style.colormap, ...COLORMAPS])) {
      select.add(new Option(name, name, false, name === style.colormap));
    }

    const inputs = style.rescale.map((value, i) => {
      const input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      input.value = Number(value.toPrecision(6));
      input.title = i === 0 ? "Minimum" : "Maximum";
      return input;
    });

    function apply() {
      const rescale = inputs.map((input) => parseFloat(input.value));
      if (rescale.some((v) => !Number.isFinite(v))) return;
      style.colormap = select.value;
      style.rescale = rescale;
      onChange();
    }
    select.addEventListener("change", apply);
    inputs.forEach((input) => input.addEventListener("change", apply));

    const range = document.createElement("div");
    range.className = "range";
    range.append(inputs[0], "–", inputs[1]);
    return [select, range];
  }

  function styleSection(title, open) {
    const details = document.createElement("details");
    details.className = "style";
    details.open = open;
    const summary = document.createElement("summary");
    summary.textContent = title;
    details.append(summary);
    return details;
  }

  function renderStyleControls(upload) {
    const info = upload.raster_info;
    const style = { colormap: info.colormap, rescale: [...info.rescale] };
    rasterStyles.set(upload.id, style);

    const details = styleSection("Style", false);
    details.append(...colorControls(style, () => restyleRaster(upload)));
    return details;
  }

  function renderZarrControls(upload) {
    const info = upload.raster_info;
    // Start each dimension where the server's defaults do (latest time, first depth).
    const defaultSel = (name) =>
      Object.fromEntries(info.variables[name].dims.map((dim) => [dim.name, dim.default ?? 0]));
    const style = {
      colormap: info.colormap,
      variable: info.default_variable,
      rescale: [...info.variables[info.default_variable].rescale],
      sel: defaultSel(info.default_variable),
    };
    rasterStyles.set(upload.id, style);

    const details = styleSection("Data", true);
    const body = document.createElement("div");
    details.append(body);

    function dimSlider(dim) {
      const wrap = document.createElement("div");
      wrap.className = "dim";
      const label = document.createElement("label");
      const units = dim.units && !dim.units.includes(" since ") ? ` ${dim.units}` : "";
      const showLabel = (i) => (label.textContent = `${dim.name}: ${dim.values[i]}${units}`);
      showLabel(style.sel[dim.name] ?? 0);
      wrap.append(label);

      if (dim.values.length > 1) {
        const slider = document.createElement("input");
        slider.type = "range";
        slider.min = 0;
        slider.max = dim.values.length - 1;
        slider.value = style.sel[dim.name] ?? 0;
        // Update the label while dragging; fetch new tiles only on release.
        slider.addEventListener("input", () => showLabel(slider.value));
        slider.addEventListener("change", () => {
          style.sel[dim.name] = Number(slider.value);
          restyleRaster(upload);
        });
        wrap.append(slider);
      }
      return wrap;
    }

    function build() {
      const variable = info.variables[style.variable];
      const parts = [];

      const names = Object.keys(info.variables);
      if (names.length > 1) {
        const select = document.createElement("select");
        for (const name of names) {
          select.add(new Option(info.variables[name].long_name, name, false, name === style.variable));
        }
        select.addEventListener("change", () => {
          style.variable = select.value;
          style.rescale = [...info.variables[select.value].rescale];
          style.sel = defaultSel(select.value);
          build();
          restyleRaster(upload);
        });
        parts.push(select);
      }

      parts.push(...variable.dims.map(dimSlider));
      parts.push(...colorControls(style, () => restyleRaster(upload)));
      if (variable.units) {
        const units = document.createElement("p");
        units.className = "hint";
        units.textContent = `Units: ${variable.units}`;
        parts.push(units);
      }
      body.replaceChildren(...parts);
    }

    build();
    return details;
  }

  // Upload id -> upload, for the layers listed in the panel (used by "Values on click").
  const uploadsById = new Map();

  function renderUpload(upload, { visible = false } = {}) {
    uploadsById.set(upload.id, upload);
    const li = document.createElement("li");
    li.dataset.id = upload.id;

    const row = document.createElement("div");
    row.className = "row";
    li.append(row);

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = upload.name;

    if (upload.status === "ready") {
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = visible;
      checkbox.title = "Show / hide";
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          showLayer(upload, false).catch((err) => setStatus(`Could not load ${upload.name}: ${err}`, true));
        } else {
          hideLayer(upload.id);
        }
      });

      const swatch = document.createElement("span");
      swatch.className = "swatch";
      if (isImagery(upload) || isTerrain(upload)) {
        swatch.classList.add("raster");
      } else {
        swatch.style.background = COLORS[upload.id % COLORS.length];
      }

      name.title = describe(upload);
      if (upload.expires_at) {
        name.textContent = `${upload.name} ⏳`;
        name.title += `\nSent from your computer; deleted ${new Date(upload.expires_at).toLocaleString()}.`;
      }

      const zoom = document.createElement("button");
      zoom.type = "button";
      zoom.textContent = "Zoom";
      zoom.addEventListener("click", () => {
        checkbox.checked = true;
        showLayer(upload, true).catch((err) => setStatus(`Could not load ${upload.name}: ${err}`, true));
      });

      row.append(checkbox, swatch, name, zoom);
      if (isGridded(upload)) {
        li.append(renderZarrControls(upload));
      } else if (upload.kind === "raster" && !upload.raster_info.rgb) {
        li.append(renderStyleControls(upload));
      }
    } else if (upload.status === "pending") {
      li.classList.add("pending");
      name.textContent = `${upload.name} (converting… reload when done)`;
      row.append(name);
    } else {
      li.classList.add("failed");
      name.title = upload.error || upload.status;
      row.append(name);
    }

    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "✕";
    del.title = "Delete";
    li.removeLayer = async () => {
      const url = urlFor(window.VIEWER_URLS.detail, upload.id);
      const response = await fetch(url, { method: "DELETE", headers: { "X-CSRFToken": csrfToken } });
      if (!response.ok) throw new Error(`Delete failed (${response.status}).`);
      removeLayer(upload);
      rasterStyles.delete(upload.id);
      li.remove();
    };
    del.addEventListener("click", async () => {
      if (!confirm(`Delete "${upload.name}"?`)) return;
      await li.removeLayer().catch((err) => setStatus(err.message, true));
    });
    row.append(del);

    return li;
  }

  async function loadUploads() {
    const response = await fetch(window.VIEWER_URLS.list);
    const { uploads } = await readJson(response);
    listEl.replaceChildren(...uploads.reverse().map((u) => renderUpload(u)));
  }

  // The data folder being shown; null for the list of data folders itself.
  let browseParent = null;

  async function browse(path) {
    const query = path ? `?${new URLSearchParams({ path })}` : "";
    const response = await fetch(window.VIEWER_URLS.browse + query);
    const listing = await readJson(response);
    if (!response.ok) throw new Error(listing.error);

    browseParent = listing.parent;
    browseUp.disabled = listing.path === null;
    browsePath.textContent = listing.path || "Data folders";
    browsePath.title = listing.path || "";

    const items = listing.entries.map((entry) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "entry-name";
      name.textContent = entry.dir ? `📁 ${entry.name}` : entry.name;
      name.title = entry.path;
      li.append(name);
      if (entry.kind) {
        const kind = document.createElement("span");
        kind.className = "entry-kind";
        kind.textContent = KIND_LABELS[entry.kind];
        li.append(kind);
      }
      li.addEventListener("click", () => {
        if (entry.dir) {
          browse(entry.path).catch((err) => setStatus(err.message, true));
          return;
        }
        browser.close();
        openAll([entry.path], {
          names: [form.elements.name.value.trim()],
          kinds: [entry.kind],
        });
      });
      return li;
    });
    if (!items.length || listing.truncated) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = !items.length
        ? listing.path === null
          ? "No data folders. Add DATA_ROOTS to .env and restart."
          : "Nothing the viewer can show here."
        : "Only the first 1000 entries are shown.";
      items.push(li);
    }
    browseList.replaceChildren(...items);
  }

  browseUp.addEventListener("click", () => browse(browseParent).catch((err) => setStatus(err.message, true)));
  document.getElementById("browse-cancel").addEventListener("click", () => browser.close());
  // Opens where it was last left; the first time, on the list of data folders.
  let browsed = false;
  async function showBrowser() {
    browser.showModal();
    if (browsed) return;
    browsed = true;
    await browse(null).catch((err) => setStatus(`Could not list data folders: ${err.message || err}`, true));
  }

  // The system file dialog, opened by the server, so the real path comes back and the file
  // is read in place. It only works with the viewer on this computer; elsewhere, fall back
  // to browsing the server's data folders in the page.
  let useSystemDialog = true;
  async function pick(folder) {
    if (!useSystemDialog) return showBrowser();
    setStatus(folder ? "Choose a folder in the dialog…" : "Choose a file in the dialog…");
    const data = new FormData();
    if (folder) data.set("folder", "1");
    try {
      const response = await fetch(window.VIEWER_URLS.pick, {
        method: "POST", body: data, headers: { "X-CSRFToken": csrfToken },
      });
      const body = await readJson(response);
      if (body.remote) {
        useSystemDialog = false;
        setStatus("");
        return showBrowser();
      }
      if (!response.ok) throw new Error(body.error);
      if (!body.path) {
        setStatus("");
        return;
      }
      form.elements.source.value = body.path;
      if (await openAll([body.path], { names: [form.elements.name.value.trim()] })) {
        form.elements.source.value = "";
        form.elements.name.value = "";
      }
    } catch (err) {
      setStatus(`Could not open the file dialog: ${err.message || err}`, true);
    }
  }
  document.getElementById("browse-button").addEventListener("click", () => pick(false));
  // Without the system dialog (viewer hosted elsewhere), there is no Folder… button.
  const folderButton = document.getElementById("folder-button");
  if (folderButton) folderButton.addEventListener("click", () => pick(true));
  else useSystemDialog = false;

  function urlFor(template, id) {
    return template.replace("/0/", `/${id}/`);
  }

  async function postForm(url, data) {
    const response = await fetch(url, { method: "POST", body: data, headers: { "X-CSRFToken": csrfToken } });
    const body = await readJson(response);
    if (!response.ok && !body.upload) throw new Error(body.error || `Request failed (${response.status}).`);
    return { response, body };
  }

  // Django answers some failures (crashes, request limits, CSRF) with an HTML page, not JSON.
  async function readJson(response) {
    const text = await response.text();
    try {
      return JSON.parse(text);
    } catch {
      const title = new DOMParser().parseFromString(text, "text/html").title.trim();
      throw new Error(
        `Server error ${response.status}${title ? `: ${title}` : ""}. See the runserver terminal for details.`
      );
    }
  }

  // A URL is http(s):// or s3://; anything else is a path on the server (Windows or
  // Linux spelling, or file://), which the server checks.
  function sourceFields(value) {
    return /^(https?|s3):\/\//i.test(value) ? { url: value } : { path: value };
  }

  // Several sources (and names, types, CRSs to match) are separated by ";".
  function splitList(value) {
    return (value || "").split(";").map((item) => item.trim());
  }

  let opening = false;

  // Open sources one after another, each as a layer, and show them. options.names,
  // .types and .crs line up with sources by position; blanks mean "work it out".
  async function openAll(sources, { names = [], types = [], crs = [], kinds = [] } = {}) {
    if (opening) return;
    opening = true;
    const failed = [];
    const items = sources.map((source, i) => ({ source, i })).filter(({ source }) => source);
    try {
      for (const [n, { source, i }] of items.entries()) {
        const label = source.split(/[\\/]/).filter(Boolean).pop() || source;
        const count = items.length > 1 ? ` (${n + 1} of ${items.length})` : "";
        const slow = kinds[i] === "pointcloud" || /\.la[sz]$/i.test(source);
        setStatus(
          `Opening ${label}${count}…` +
            (slow ? " point clouds are converted to 3D Tiles the first time, which can take minutes." : "")
        );
        const error = await openLayer({
          ...sourceFields(source), name: names[i] || "", type: types[i] || "", source_crs: crs[i] || "",
        });
        if (error) failed.push(`${label}: ${error}`);
      }
    } finally {
      opening = false;
    }
    if (failed.length) {
      const opened = items.length - failed.length;
      setStatus((items.length > 1 ? `Opened ${opened} of ${items.length}. ` : "") + failed.join(" · "), true);
    }
    return failed.length === 0;
  }

  // Open one source as a layer and show it; returns an error message, or null.
  async function openLayer(fields) {
    const data = new FormData();
    for (const [key, value] of Object.entries(fields)) if (value) data.set(key, value);
    try {
      const { response, body } = await postForm(window.VIEWER_URLS.open, data);
      if (body.folder) {
        showFolder(body.folder);
        return null;
      }
      if (body.upload) {
        // Opening a source again returns its existing layer; don't list it twice.
        listEl.querySelector(`li[data-id="${body.upload.id}"]`)?.remove();
        listEl.append(renderUpload(body.upload, { visible: response.ok }));
      }
      if (!response.ok) return body.error || `request failed (${response.status})`;
      const upload = body.upload;
      setStatus(
        upload.kind === "pointcloud"
          ? `Opened ${upload.raster_info.points.toLocaleString()} points in ${upload.detected_crs}.`
          : isGridded(upload)
          ? `Opened ${KIND_LABELS[upload.kind]}: ${Object.keys(upload.raster_info.variables).join(", ")}.`
          : upload.kind === "pmtiles"
          ? `Opened PMTiles (${upload.raster_info.tile_type}).`
          : upload.kind === "raster"
          ? `Opened raster in ${upload.detected_crs}.`
          : `Opened ${upload.feature_count} features from ${upload.detected_crs}.`
      );
      await showLayer(upload, true);
      return null;
    } catch (err) {
      return err.message || String(err);
    }
  }

  // ---- Folders ---------------------------------------------------------------------
  // Opening a folder shows its plottable files in a window in the middle of the screen:
  // tick some and plot them. The folder stays as a row in the panel (remembered in this
  // browser) that reopens the window.
  const FOLDERS_KEY = "geodata-viewer.folders";
  const folderDialog = document.getElementById("folder-dialog");
  const folderTitle = document.getElementById("folder-title");
  const folderPath = document.getElementById("folder-path");
  const folderUp = document.getElementById("folder-up");
  const folderFiles = document.getElementById("folder-files");
  const folderAll = document.getElementById("folder-all");
  const folderPlot = document.getElementById("folder-plot");
  // The folder the window was opened on, and the listing it shows (maybe a subfolder).
  let folderRoot = null;
  let folderListing = null;

  function savedFolders() {
    try {
      return JSON.parse(localStorage.getItem(FOLDERS_KEY)) || [];
    } catch {
      return [];
    }
  }

  function saveFolders(roots) {
    try {
      localStorage.setItem(FOLDERS_KEY, JSON.stringify(roots));
    } catch {
      // Private window or storage blocked: folders just aren't remembered.
    }
  }

  async function fetchFolder(source) {
    const data = new FormData();
    for (const [key, value] of Object.entries(sourceFields(source))) data.set(key, value);
    const { response, body } = await postForm(window.VIEWER_URLS.open, data);
    if (!response.ok) throw new Error(body.error);
    if (!body.folder) throw new Error(`${source} is not a folder.`);
    return body.folder;
  }

  function folderName(path) {
    return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
  }

  // A folder listing arrived (from the path box, a link or Folder…): list it in the
  // panel and show its files.
  function showFolder(listing) {
    addFolderRow(listing.path);
    if (!savedFolders().includes(listing.path)) saveFolders([...savedFolders(), listing.path]);
    openFolderWindow(listing.path, listing);
    setStatus("");
  }

  async function openFolderWindow(root, listing) {
    folderRoot = root;
    folderTitle.textContent = folderName(root);
    folderTitle.title = root;
    if (!folderDialog.open) folderDialog.showModal();
    if (listing) {
      renderFolderListing(listing);
      return;
    }
    folderFiles.replaceChildren(folderMessage("Loading…"));
    try {
      renderFolderListing(await fetchFolder(root));
    } catch (err) {
      folderFiles.replaceChildren(folderMessage(`Could not open ${root}: ${err.message || err}`));
    }
  }

  function folderMessage(text) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = text;
    return li;
  }

  const folderBoxes = () => [...folderFiles.querySelectorAll("input[type=checkbox]")];

  function updateFolderPlot() {
    const ticked = folderBoxes().filter((b) => b.checked).length;
    folderPlot.disabled = ticked === 0;
    folderPlot.textContent = ticked > 1 ? `Plot ${ticked} selected` : "Plot selected";
    folderAll.checked = ticked > 0 && ticked === folderBoxes().length;
  }

  function renderFolderListing(listing) {
    folderListing = listing;
    folderUp.disabled = listing.path === folderRoot || !listing.parent;
    folderPath.textContent = listing.path;
    folderPath.title = listing.path;
    const items = listing.entries.map((entry) => {
      const item = document.createElement("li");
      if (entry.dir) {
        item.className = "subfolder";
        item.textContent = `📁 ${entry.name}`;
        item.title = entry.path;
        item.addEventListener("click", () => loadFolderListing(entry.path));
        return item;
      }
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = entry.path;
      box.dataset.kind = entry.kind;
      box.addEventListener("change", updateFolderPlot);
      const text = document.createElement("span");
      text.className = "entry-name";
      text.textContent = entry.name;
      text.title = entry.path;
      const kind = document.createElement("span");
      kind.className = "entry-kind";
      kind.textContent = KIND_LABELS[entry.kind] || entry.kind;
      label.append(box, text, kind);
      item.append(label);
      return item;
    });
    if (!listing.entries.some((e) => !e.dir)) items.push(folderMessage("No files here the viewer can plot."));
    if (listing.truncated) items.push(folderMessage("Only the first 1000 entries are shown."));
    folderFiles.replaceChildren(...items);
    updateFolderPlot();
  }

  async function loadFolderListing(source) {
    try {
      renderFolderListing(await fetchFolder(source));
    } catch (err) {
      folderFiles.replaceChildren(folderMessage(`Could not open ${source}: ${err.message || err}`));
    }
  }

  folderUp.addEventListener("click", () => folderListing?.parent && loadFolderListing(folderListing.parent));
  folderAll.addEventListener("change", () => {
    folderBoxes().forEach((b) => (b.checked = folderAll.checked));
    updateFolderPlot();
  });
  document.getElementById("folder-cancel").addEventListener("click", () => folderDialog.close());
  folderPlot.addEventListener("click", () => {
    const ticked = folderBoxes().filter((b) => b.checked);
    folderDialog.close();
    setPanelOpen(true);
    openAll(ticked.map((b) => b.value), { kinds: ticked.map((b) => b.dataset.kind) });
  });

  // The folder's row in the panel: click it to choose more files to plot.
  function addFolderRow(root) {
    if ([...folderListEl.children].some((li) => li.dataset.root === root)) return;
    const li = document.createElement("li");
    li.className = "folder";
    li.dataset.root = root;
    const row = document.createElement("div");
    row.className = "row";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = `📁 ${folderName(root)}`;
    name.title = `${root}\nClick to choose files to plot.`;
    name.addEventListener("click", () => openFolderWindow(root));
    const open = document.createElement("button");
    open.type = "button";
    open.textContent = "Files…";
    open.title = "Choose files from this folder to plot";
    open.addEventListener("click", () => openFolderWindow(root));
    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "✕";
    del.title = "Remove this folder from the panel (files are not touched)";
    del.addEventListener("click", () => {
      li.remove();
      saveFolders(savedFolders().filter((r) => r !== root));
    });
    row.append(name, open, del);
    li.append(row);
    folderListEl.append(li);
  }

  // Folders from earlier visits; their files are listed when opened.
  function restoreFolders() {
    for (const root of savedFolders()) addFolderRow(root);
  }

  // Remove every layer (server layers are deleted, temporary files with them); folders stay.
  document.getElementById("clear-layers").addEventListener("click", async () => {
    const rows = [...listEl.children].filter((li) => li.removeLayer);
    if (!rows.length || !confirm(`Remove all ${rows.length} layer${rows.length === 1 ? "" : "s"}?`)) return;
    const failed = [];
    for (const li of rows) await Promise.resolve(li.removeLayer()).catch((err) => failed.push(err.message));
    setStatus(failed.length ? `${failed.length} layer(s) could not be removed: ${failed[0]}` : "All layers removed.", failed.length > 0);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const sources = splitList(form.elements.source.value);
    if (!sources.some(Boolean)) {
      setStatus("Choose a file with Browse…, or enter paths or URLs separated by ;", true);
      return;
    }
    const ok = await openAll(sources, {
      names: splitList(form.elements.name.value),
    });
    if (ok) {
      form.elements.source.value = "";
      form.elements.name.value = "";
    }
  });

  // ---- Files from this computer -----------------------------------------------------
  // GeoJSON, shapefiles, GeoTIFFs and raster PMTiles are read by the browser and never
  // leave it (layers last for this tab). Other files, or ones the browser can't read, are
  // sent to the server for a quick look and deleted there after a while.

  const crsDefinitions = new Map();
  async function getDefinition(code) {
    if (!crsDefinitions.has(code)) {
      const response = await fetch(`${window.VIEWER_URLS.crs}?${new URLSearchParams({ code })}`);
      const body = await readJson(response);
      if (!response.ok) throw new Error(body.error);
      crsDefinitions.set(code, body.proj4);
    }
    return crsDefinitions.get(code);
  }

  // Files picked together: a .shp travels with its .dbf/.prj/.shx/.cpg.
  function groupFiles(files) {
    const byStem = new Map();
    const items = [];
    for (const file of files) {
      const match = /^(.*)(\.(shp|shx|dbf|prj|cpg))$/i.exec(file.name);
      if (!match) {
        items.push({ name: file.name, file });
        continue;
      }
      const stem = match[1].toLowerCase();
      if (!byStem.has(stem)) byStem.set(stem, {});
      byStem.get(stem)[match[2].toLowerCase()] = file;
    }
    for (const parts of byStem.values()) {
      if (parts[".shp"]) items.push({ name: parts[".shp"].name, parts });
    }
    return items;
  }

  async function openLocalFiles(files) {
    if (opening) return;
    opening = true;
    const failed = [];
    const items = groupFiles([...files]);
    try {
      for (const [n, item] of items.entries()) {
        const count = items.length > 1 ? ` (${n + 1} of ${items.length})` : "";
        setStatus(`Opening ${item.name}${count}…`);
        const error = await openLocalItem(item);
        if (error) failed.push(`${item.name}: ${error}`);
      }
    } finally {
      opening = false;
      localInput.value = "";
    }
    if (failed.length) {
      const opened = items.length - failed.length;
      setStatus((items.length > 1 ? `Opened ${opened} of ${items.length}. ` : "") + failed.join(" · "), true);
    }
  }

  // Returns an error message, or null.
  async function openLocalItem(item, crs) {
    const kind = item.parts ? "shapefile" : LocalFiles.browserKind(item.name);
    if (kind) {
      try {
        await showLocal(item, kind, crs);
        return null;
      } catch (err) {
        // A .shp with sidecars can't be sent as one file; everything else can.
        if (item.parts) return err.message || String(err);
        setStatus(`The browser can't show ${item.name} (${err.message || err}); sending it to the server…`);
      }
    }
    return sendToServer(item.file, crs);
  }

  async function showLocal(item, kind, crs) {
    const name = item.name.replace(/\.(geojson|json|zip|shp|tiff?|pmtiles)$/i, "");
    let handle;
    let description;
    if (kind === "geojson" || kind === "shapefile") {
      const { collection, crs: found } =
        kind === "geojson"
          ? await LocalFiles.readGeoJSON(await item.file.text(), { crs, getDefinition })
          : await LocalFiles.readShapefile(item.parts || { ".zip": item.file }, { crs, getDefinition });
      handle = await localVector(collection, name);
      description = `${collection.features.length} features, from ${found}`;
    } else if (kind === "geotiff") {
      const image = await LocalFiles.readGeoTIFF(item.file, { crs, getDefinition });
      handle = await localImage(image, name);
      description = `GeoTIFF, ${image.rgb ? "RGB" : `${image.bands} band${image.bands > 1 ? "s" : ""}`}, ${image.crs}`;
    } else {
      const archive = await LocalFiles.openPMTiles(item.file);
      handle = localTiles(archive, name);
      description = `PMTiles, zoom ${archive.minzoom}-${archive.maxzoom}`;
    }
    listEl.append(renderLocal(name, `${description}. Read by your browser; not uploaded.`, handle));
    await handle.zoom();
    setStatus(`Opened ${item.name} (${description}).`);
  }

  // A layer read in the browser: { show(visible), remove(), zoom() }.
  async function localVector(collection, name) {
    const color = colorFor(++localCount);
    const source = await Cesium.GeoJsonDataSource.load(collection, {
      stroke: color,
      fill: color.withAlpha(0.4),
      markerColor: color,
      strokeWidth: 3,
      clampToGround: true,
    });
    styleLines(source, color);
    source.name = name;
    await viewer.dataSources.add(source);
    return {
      color,
      show: (visible) => (source.show = visible),
      remove: () => viewer.dataSources.remove(source, true),
      zoom: () => viewer.flyTo(source),
    };
  }

  async function localImage(image, name) {
    const layers = [];
    for (const part of image.parts) {
      const canvas = document.createElement("canvas");
      canvas.width = part.width;
      canvas.height = part.height;
      canvas.getContext("2d").putImageData(new ImageData(part.rgba, part.width, part.height), 0, 0);
      const url = URL.createObjectURL(await new Promise((resolve) => canvas.toBlob(resolve)));
      const provider = await Cesium.SingleTileImageryProvider.fromUrl(url, {
        rectangle: Cesium.Rectangle.fromDegrees(part.west, part.south, part.east, part.north),
        credit: name,
      });
      layers.push({ layer: viewer.imageryLayers.addImageryProvider(provider), url });
    }
    return {
      show: (visible) => layers.forEach(({ layer }) => (layer.show = visible)),
      remove: () =>
        layers.forEach(({ layer, url }) => {
          viewer.imageryLayers.remove(layer, true);
          URL.revokeObjectURL(url);
        }),
      zoom: () => viewer.camera.flyTo({ destination: Cesium.Rectangle.fromDegrees(...image.bounds) }),
    };
  }

  const EMPTY_TILE = document.createElement("canvas");
  EMPTY_TILE.width = EMPTY_TILE.height = 1;

  function localTiles(archive, name) {
    const [west, south, east, north] = archive.bounds;
    const provider = new Cesium.UrlTemplateImageryProvider({
      url: "local-pmtiles/{z}/{x}/{y}",
      rectangle: west > east ? undefined : Cesium.Rectangle.fromDegrees(west, south, east, north),
      maximumLevel: archive.maxzoom,
      tileWidth: archive.tileSize,
      tileHeight: archive.tileSize,
      credit: name,
    });
    // Tiles come from the local file instead of a URL.
    provider.requestImage = async (x, y, level) => {
      const blob = level >= archive.minzoom ? await archive.getTile(level, x, y) : null;
      return blob ? createImageBitmap(blob) : EMPTY_TILE;
    };
    const layer = viewer.imageryLayers.addImageryProvider(provider);
    return {
      show: (visible) => (layer.show = visible),
      remove: () => viewer.imageryLayers.remove(layer, true),
      zoom: () => viewer.camera.flyTo({ destination: Cesium.Rectangle.fromDegrees(west, south, east, north) }),
    };
  }

  let localCount = 0;

  function renderLocal(name, description, handle) {
    const li = document.createElement("li");
    li.className = "local";
    const row = document.createElement("div");
    row.className = "row";
    li.append(row);

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = true;
    checkbox.title = "Show / hide";
    checkbox.addEventListener("change", () => handle.show(checkbox.checked));

    const swatch = document.createElement("span");
    swatch.className = "swatch";
    if (handle.color) swatch.style.background = handle.color.toCssColorString();
    else swatch.classList.add("raster");

    const label = document.createElement("span");
    label.className = "name";
    label.textContent = `${name} 💻`;
    label.title = `${name} (${description})`;

    const zoom = document.createElement("button");
    zoom.type = "button";
    zoom.textContent = "Zoom";
    zoom.addEventListener("click", () => {
      checkbox.checked = true;
      handle.show(true);
      handle.zoom();
    });

    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "✕";
    del.title = "Remove";
    li.removeLayer = () => {
      handle.remove();
      li.remove();
    };
    del.addEventListener("click", () => li.removeLayer());
    row.append(checkbox, swatch, label, zoom, del);
    return li;
  }

  // Send a file to the server for a quick look, with upload progress; error or null.
  function sendToServer(file, crs) {
    if (file.size > window.MAX_UPLOAD_SIZE) {
      const limit = Math.floor(window.MAX_UPLOAD_SIZE / 1024 / 1024);
      return Promise.resolve(`larger than the server accepts (${limit} MB).`);
    }
    const data = new FormData();
    data.set("file", file);
    if (crs) data.set("source_crs", crs);
    return new Promise((resolve) => {
      const request = new XMLHttpRequest();
      request.open("POST", window.VIEWER_URLS.temp);
      request.setRequestHeader("X-CSRFToken", csrfToken);
      request.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        const percent = Math.round((event.loaded / event.total) * 100);
        setStatus(
          percent < 100
            ? `Sending ${file.name} to the server: ${percent}%…`
            : `Reading ${file.name} on the server…` +
                (/\.la[sz]$/i.test(file.name) ? " point clouds take a few minutes to convert." : "")
        );
      });
      request.addEventListener("load", async () => {
        let body;
        try {
          body = JSON.parse(request.responseText);
        } catch {
          return resolve(`server error ${request.status}.`);
        }
        if (request.status !== 201) return resolve(body.error || `request failed (${request.status}).`);
        listEl.append(renderUpload(body.upload, { visible: true }));
        setStatus(`Opened ${file.name}; the server deletes it after ${window.TEMP_UPLOAD_HOURS ?? 24} hours.`);
        try {
          await showLayer(body.upload, true);
          resolve(null);
        } catch (err) {
          resolve(err.message || String(err));
        }
      });
      request.addEventListener("error", () => resolve("the upload failed (network error)."));
      request.send(data);
    });
  }

  const localInput = document.getElementById("local-input");
  localInput.addEventListener("change", () => openLocalFiles(localInput.files));
  document.getElementById("local-button")?.addEventListener("click", () => localInput.click());

  // Files dropped on the globe.
  const globe = document.getElementById("cesiumContainer");
  globe.addEventListener("dragover", (event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  globe.addEventListener("drop", (event) => {
    event.preventDefault();
    if (event.dataTransfer.files.length) openLocalFiles(event.dataTransfer.files);
  });

  // ---- Map controls ------------------------------------------------------------------
  // Zoom, tilt and rotate around the point in the middle of the view; the compass shows
  // north and resets it.
  const { camera, scene } = viewer;
  const TURN = Cesium.Math.toRadians(22.5);
  const TILT = Cesium.Math.toRadians(15);
  const MIN_PITCH = Cesium.Math.toRadians(-90); // looking straight down
  const MAX_PITCH = Cesium.Math.toRadians(-10); // nearly level with the horizon

  function viewCentre() {
    const middle = new Cesium.Cartesian2(scene.canvas.clientWidth / 2, scene.canvas.clientHeight / 2);
    const ray = camera.getPickRay(middle);
    return (ray && scene.globe.pick(ray, scene)) || camera.pickEllipsoid(middle, scene.globe.ellipsoid);
  }

  // Fly to a new heading / pitch / distance around the view centre.
  function orbit({ heading = camera.heading, pitch = camera.pitch, scale = 1 }) {
    const centre = viewCentre();
    if (!centre) {
      // Looking past the globe: no centre to orbit, so only zoom.
      if (scale < 1) camera.zoomIn(camera.positionCartographic.height * (1 - scale));
      else if (scale > 1) camera.zoomOut(camera.positionCartographic.height * (scale - 1));
      return;
    }
    const range = Cesium.Cartesian3.distance(camera.position, centre) * scale;
    camera.flyToBoundingSphere(new Cesium.BoundingSphere(centre, 0), {
      offset: new Cesium.HeadingPitchRange(heading, Cesium.Math.clamp(pitch, MIN_PITCH, MAX_PITCH), range),
      duration: 0.35,
    });
  }

  const controls = {
    "zoom-in": () => orbit({ scale: 0.5 }),
    "zoom-out": () => orbit({ scale: 2 }),
    "tilt-down": () => orbit({ pitch: camera.pitch + TILT }),
    "tilt-up": () => orbit({ pitch: camera.pitch - TILT }),
    "rotate-left": () => orbit({ heading: camera.heading - TURN }),
    "rotate-right": () => orbit({ heading: camera.heading + TURN }),
    compass: () => orbit({ heading: 0 }),
  };
  for (const [id, action] of Object.entries(controls)) {
    document.getElementById(id).addEventListener("click", action);
  }

  const needle = document.querySelector("#compass svg");
  const tiltDown = document.getElementById("tilt-down");
  const tiltUp = document.getElementById("tilt-up");
  let shownHeading = null;
  scene.postRender.addEventListener(() => {
    if (camera.heading !== shownHeading) {
      shownHeading = camera.heading;
      needle.style.transform = `rotate(${-shownHeading}rad)`;
    }
    tiltDown.disabled = camera.pitch >= MAX_PITCH - 0.01;
    tiltUp.disabled = camera.pitch <= MIN_PITCH + 0.01;
  });

  // ---- Measuring distances -------------------------------------------------------------
  // Click points on the map; each point is labelled with the distance so far. Double-click
  // or Esc finishes; the ruler button again clears it.
  const measureButton = document.getElementById("measure");
  const events = new Cesium.ScreenSpaceEventHandler(scene.canvas);
  // Cesium's double-click flies to the clicked entity; double-click finishes measuring here.
  viewer.cesiumWidget.screenSpaceEventHandler.removeInputAction(Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);
  let measure = null; // { points, cursor, entities, done }

  // The point on the globe under the cursor: on the terrain where it has loaded, else on
  // the plain globe surface, so measuring works anywhere, with or without data.
  function groundAt(position) {
    const ray = camera.getPickRay(position);
    return (ray && scene.globe.pick(ray, scene)) || camera.pickEllipsoid(position, scene.globe.ellipsoid);
  }

  function formatDistance(metres) {
    return metres < 1000 ? `${metres.toFixed(1)} m` : `${(metres / 1000).toFixed(metres < 100000 ? 3 : 1)} km`;
  }

  function pathLength(points) {
    let total = 0;
    for (let i = 1; i < points.length; i++) {
      const geodesic = new Cesium.EllipsoidGeodesic(
        Cesium.Cartographic.fromCartesian(points[i - 1]),
        Cesium.Cartographic.fromCartesian(points[i])
      );
      total += geodesic.surfaceDistance;
    }
    return total;
  }

  function startMeasuring() {
    hideIdentify();
    measure = { points: [], cursor: null, entities: [], done: false };
    const line = viewer.entities.add({
      polyline: {
        positions: new Cesium.CallbackProperty(
          () => (measure.cursor && !measure.done ? [...measure.points, measure.cursor] : measure.points),
          false
        ),
        width: 3,
        material: Cesium.Color.fromCssColorString("#ffcc00"),
        clampToGround: true,
      },
    });
    measure.entities.push(line);
    measureButton.classList.add("active");
    scene.canvas.style.cursor = "crosshair";
    setStatus("Measuring: click points on the map; double-click or Esc to finish.");
  }

  function finishMeasuring() {
    if (!measure || measure.done) return;
    measure.done = true;
    scene.canvas.style.cursor = "";
    const total = pathLength(measure.points);
    setStatus(measure.points.length > 1 ? `Distance: ${formatDistance(total)}. Click the ruler to clear.` : "");
  }

  function clearMeasuring() {
    if (!measure) return;
    measure.entities.forEach((entity) => viewer.entities.remove(entity));
    measure = null;
    measureButton.classList.remove("active");
    scene.canvas.style.cursor = "";
    setStatus("");
  }

  function addMeasurePoint(position) {
    const last = measure.points[measure.points.length - 1];
    // The two clicks of a double-click land on the same spot: count it once.
    if (last && Cesium.Cartesian3.distance(last, position) < 0.01) return;
    measure.points.push(position);
    const distance = pathLength(measure.points);
    measure.entities.push(
      viewer.entities.add({
        position,
        point: { pixelSize: 7, color: Cesium.Color.WHITE, outlineColor: Cesium.Color.BLACK, outlineWidth: 1,
                 heightReference: Cesium.HeightReference.CLAMP_TO_GROUND, disableDepthTestDistance: Number.POSITIVE_INFINITY },
        label: measure.points.length > 1 ? {
          text: formatDistance(distance),
          font: "13px system-ui, sans-serif",
          fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -16),
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        } : undefined,
      })
    );
    setStatus(`Measuring: ${formatDistance(distance)}. Double-click or Esc to finish.`);
  }

  measureButton.addEventListener("click", () => (measure ? clearMeasuring() : startMeasuring()));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && measure && !measure.done) finishMeasuring();
  });
  events.setInputAction((movement) => {
    if (measure && !measure.done && measure.points.length) measure.cursor = groundAt(movement.endPosition) || measure.cursor;
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
  events.setInputAction(() => finishMeasuring(), Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

  // ---- Values on click ---------------------------------------------------------------
  // Clicking the map shows the location and, for every visible layer, its
  // value there: raster bands, gridded variables (at the chosen time/depth), the clicked
  // vector feature's properties, a point cloud's height.
  const identifyEl = document.getElementById("identify");
  const identifyBody = identifyEl.querySelector(".identify-body");
  identifyEl.querySelector(".close").addEventListener("click", hideIdentify);
  // The clicked spot: the popup stays next to it as the map moves.
  let identifyAt = null;

  function hideIdentify() {
    identifyEl.hidden = true;
    identifyAt = null;
  }

  scene.postRender.addEventListener(() => {
    if (!identifyAt || identifyEl.hidden) return;
    const position = Cesium.SceneTransforms.worldToWindowCoordinates(scene, identifyAt);
    if (position) placeIdentify(position);
    else hideIdentify(); // the spot went behind the globe
  });

  function formatValue(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "number") {
      return Math.abs(value) >= 1e5 || (value !== 0 && Math.abs(value) < 1e-3)
        ? value.toExponential(4)
        : String(Number(value.toFixed(4)));
    }
    return String(value);
  }

  // A titled table; rows is [[name, value], ...]. Returns its value cells, to fill later.
  function identifySection(title, rows) {
    const section = document.createElement("section");
    const heading = document.createElement("h3");
    heading.textContent = title;
    const table = document.createElement("table");
    const cells = rows.map(([name, value]) => {
      const tr = table.insertRow();
      tr.insertCell().textContent = name;
      const cell = tr.insertCell();
      cell.textContent = formatValue(value);
      return cell;
    });
    section.append(heading, table);
    identifyBody.append(section);
    return cells;
  }

  function placeIdentify(position) {
    identifyEl.hidden = false;
    const { innerWidth: width, innerHeight: height } = window;
    const box = identifyEl.getBoundingClientRect();
    identifyEl.style.left = `${Math.max(8, Math.min(position.x + 12, width - box.width - 8))}px`;
    identifyEl.style.top = `${Math.max(8, Math.min(position.y + 12, height - box.height - 8))}px`;
  }

  async function identify(position) {
    const picked = scene.drillPick(position, 10);
    const ground = groundAt(position);
    const pointCloud = picked.some((p) => p.primitive instanceof Cesium.Cesium3DTileset);
    const where = (pointCloud && scene.pickPositionSupported && scene.pickPosition(position)) || ground;
    if (!where) {
      hideIdentify();
      return;
    }
    const carto = Cesium.Cartographic.fromCartesian(where);
    const lon = Cesium.Math.toDegrees(carto.longitude);
    const lat = Cesium.Math.toDegrees(carto.latitude);
    identifyBody.replaceChildren();
    identifySection("Location", [
      ["Latitude", lat.toFixed(6)],
      ["Longitude", lon.toFixed(6)],
    ]);
    if (pointCloud) identifySection("Point cloud", [["Point height (m)", carto.height.toFixed(2)]]);

    // Vector features under the click (the outlines drawn for polygons point at them).
    const seen = new Set();
    for (const { id: entity } of picked) {
      if (!(entity instanceof Cesium.Entity)) continue;
      const feature = entity.parent && !entity.properties ? entity.parent : entity;
      if (seen.has(feature) || !feature.properties) continue;
      seen.add(feature);
      const properties = feature.properties.getValue(Cesium.JulianDate.now()) || {};
      const layer = feature.entityCollection?.owner?.name || "Feature";
      const rows = Object.entries(properties).filter(([, v]) => typeof v !== "object" || v === null);
      identifySection(layer, rows.length ? rows : [["(no properties)", ""]]);
    }

    // Raster and gridded layers that are showing: ask the server for their values here.
    const requests = [];
    for (const li of listEl.querySelectorAll("li[data-id]")) {
      const upload = uploadsById.get(Number(li.dataset.id));
      const layer = upload && layers.get(upload.id);
      if (!layer?.show || !(upload.kind === "raster" || isGridded(upload))) continue;
      const params = new URLSearchParams({ lon, lat });
      const style = rasterStyles.get(upload.id);
      if (style?.variable) params.set("variable", style.variable);
      const sel = Object.entries(style?.sel || {}).map(([dim, index]) => `${dim}:${index}`).join(",");
      if (sel) params.set("sel", sel);
      const [cell] = identifySection(upload.name, [["", "…"]]);
      requests.push(
        fetch(`${urlFor(window.VIEWER_URLS.value, upload.id)}?${params}`)
          .then(readJson)
          .then((body) => {
            const table = cell.closest("table");
            const entries = Object.entries(body.values || {});
            table.replaceChildren();
            for (const [name, value] of entries.length ? entries : [["No data here", null]]) {
              const tr = table.insertRow();
              tr.insertCell().textContent = name;
              tr.insertCell().textContent = entries.length ? formatValue(value) : "";
            }
          })
          .catch((err) => (cell.textContent = `error: ${err.message || err}`))
      );
    }
    identifyAt = where;
    placeIdentify(position);
    await Promise.all(requests);
  }

  events.setInputAction((click) => {
    if (measure && !measure.done) {
      const position = groundAt(click.position);
      if (position) addMeasurePoint(position);
      return;
    }
    identify(click.position);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // The layer list first (it replaces the list's contents), then remembered folders, then
  // what the link asks for: links like /?url=https://…/sst.nc;C:\data\dem.tif&type=;raster
  // open those datasets, and a folder in them lists its files to choose from.
  (async () => {
    await loadUploads().catch((err) => setStatus(`Could not load layers: ${err}`, true));
    restoreFolders();
    const params = new URLSearchParams(window.location.search);
    const linked = [...splitList(params.get("url")), ...splitList(params.get("path"))];
    if (linked.some(Boolean)) {
      await openAll(linked, {
        names: splitList(params.get("name")),
        types: splitList(params.get("type")),
        crs: splitList(params.get("crs")),
      });
    }
  })();
})();
