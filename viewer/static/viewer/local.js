// Read local files in the browser for a quick look; nothing is uploaded. Uses the GeoTIFF,
// proj4, shp and pmtiles globals. No Cesium here: viewer.js turns the results into layers.
(function (root) {
  const WGS84 = "EPSG:4326";
  // Longest side of a GeoTIFF read for display; overviews are used when the file has them.
  const MAX_PIXELS = 3072;
  // Viridis, as 10 evenly spaced stops.
  const VIRIDIS = ["#440154", "#482878", "#3e4989", "#31688e", "#26828e", "#1f9e89", "#35b779", "#6ece58", "#b5de2b", "#fde725"];
  const RASTER_TILE_TYPES = { 2: "image/png", 3: "image/jpeg", 4: "image/webp", 5: "image/avif" };

  // Files the browser can show by itself; anything else goes to the server.
  function browserKind(name) {
    const lower = name.toLowerCase();
    if (lower.endsWith(".geojson") || lower.endsWith(".json")) return "geojson";
    if (lower.endsWith(".zip") || lower.endsWith(".shp")) return "shapefile";
    if (lower.endsWith(".tif") || lower.endsWith(".tiff")) return "geotiff";
    if (lower.endsWith(".pmtiles")) return "pmtiles";
    return null;
  }

  // ---- Coordinate systems ------------------------------------------------------------

  // proj4 knows only a few CRSs itself; getDefinition(code) supplies the rest (the server
  // asks PROJ). Returns a function (x, y) => [x, y] from `from` to `to`.
  async function transformer(from, to, getDefinition) {
    for (const code of [from, to]) {
      if (!proj4.defs(code)) proj4.defs(code, await getDefinition(code));
    }
    const converter = proj4(from, to);
    return (x, y) => converter.forward([x, y]);
  }

  function crsNameOf(doc) {
    const name = doc && doc.crs && doc.crs.properties && doc.crs.properties.name;
    if (!name) return null;
    const match = /EPSG:{1,2}(\d+)/i.exec(name);
    if (match) return `EPSG:${match[1]}`;
    return /CRS84/i.test(name) ? WGS84 : name;
  }

  function mapCoordinates(coords, fn) {
    if (typeof coords[0] === "number") return fn(coords[0], coords[1]).concat(coords.slice(2));
    return coords.map((c) => mapCoordinates(c, fn));
  }

  function mapGeometry(geometry, fn) {
    if (!geometry) return geometry;
    if (geometry.type === "GeometryCollection") {
      return { ...geometry, geometries: geometry.geometries.map((g) => mapGeometry(g, fn)) };
    }
    return { ...geometry, coordinates: mapCoordinates(geometry.coordinates, fn) };
  }

  // ---- GeoJSON and shapefiles ----------------------------------------------------------

  // A FeatureCollection in WGS84 from GeoJSON text; old-style "crs" members are honoured.
  async function readGeoJSON(text, { crs, getDefinition } = {}) {
    let doc;
    try {
      doc = JSON.parse(text);
    } catch (err) {
      throw new Error(`Invalid GeoJSON: ${err.message}`);
    }
    let features;
    if (doc.type === "FeatureCollection") features = doc.features || [];
    else if (doc.type === "Feature") features = [doc];
    else if (doc.coordinates || doc.type === "GeometryCollection") features = [{ type: "Feature", properties: {}, geometry: doc }];
    else throw new Error("Invalid GeoJSON: no features or geometry found.");

    const source = crs || crsNameOf(doc) || WGS84;
    if (source !== WGS84) {
      const toWgs84 = await transformer(source, WGS84, getDefinition);
      features = features.map((f) => ({ ...f, geometry: mapGeometry(f.geometry, toWgs84) }));
    }
    return { collection: { type: "FeatureCollection", features }, crs: source };
  }

  // A shapefile from a .zip, or from a .shp with its sidecars ({".shp": File, ".dbf": …}).
  // shp reprojects using the .prj; without one the coordinates must already be lon/lat.
  async function readShapefile(parts, { crs, getDefinition } = {}) {
    let geojson;
    if (parts[".zip"]) {
      geojson = await shp(await parts[".zip"].arrayBuffer());
    } else {
      if (!parts[".dbf"]) throw new Error("Choose the .dbf (and .prj) files together with the .shp.");
      const prj = parts[".prj"] ? await parts[".prj"].text() : undefined;
      const cpg = parts[".cpg"] ? await parts[".cpg"].text() : undefined;
      geojson = shp.combine([
        await shp.parseShp(await parts[".shp"].arrayBuffer(), crs ? undefined : prj),
        await shp.parseDbf(await parts[".dbf"].arrayBuffer(), cpg),
      ]);
    }
    const layers = Array.isArray(geojson) ? geojson : [geojson];
    let features = layers.flatMap((layer) => layer.features);
    if (crs) {
      const toWgs84 = await transformer(crs, WGS84, getDefinition);
      features = features.map((f) => ({ ...f, geometry: mapGeometry(f.geometry, toWgs84) }));
    } else if (!looksGeographic(features)) {
      throw new Error("The shapefile has no .prj and is not in longitude/latitude. Enter the source CRS.");
    }
    return { collection: { type: "FeatureCollection", features }, crs: crs || "from .prj" };
  }

  function looksGeographic(features) {
    let ok = true;
    for (const f of features) {
      mapGeometry(f.geometry, (x, y) => {
        if (Math.abs(x) > 360 || Math.abs(y) > 90) ok = false;
        return [x, y];
      });
      if (!ok) break;
    }
    return ok;
  }

  // ---- PMTiles ------------------------------------------------------------------------

  // A raster PMTiles archive read from the local file, tile by tile.
  async function openPMTiles(file) {
    const archive = new pmtiles.PMTiles(new pmtiles.FileSource(file));
    const header = await archive.getHeader();
    const contentType = RASTER_TILE_TYPES[header.tileType];
    if (!contentType) throw new Error("Only raster PMTiles (PNG, JPEG, WebP, AVIF) are read in the browser.");
    const metadata = (await archive.getMetadata()) || {};
    return {
      contentType,
      bounds: [header.minLon, header.minLat, header.maxLon, header.maxLat],
      minzoom: header.minZoom,
      maxzoom: header.maxZoom,
      tileSize: Number(metadata.tileSize) || 256,
      async getTile(z, x, y) {
        const tile = await archive.getZxy(z, x, y);
        return tile ? new Blob([tile.data], { type: contentType }) : null;
      },
    };
  }

  // ---- GeoTIFF ------------------------------------------------------------------------

  function crsOfImage(image) {
    const keys = image.geoKeys || {};
    const code = keys.ProjectedCSTypeGeoKey || keys.GeographicTypeGeoKey;
    if (!code || code === 32767) {
      throw new Error("The GeoTIFF's CRS has no EPSG code, which the browser needs. Enter the source CRS.");
    }
    return `EPSG:${code}`;
  }

  // The GeoTIFF as one or two images in lon/lat (two when it crosses 180°), each
  // {west, south, east, north, width, height, rgba}, plus what was found in the file.
  async function readGeoTIFF(blob, { crs, getDefinition } = {}) {
    const tiff = await GeoTIFF.fromBlob(blob);
    const image = await tiff.getImage();
    const source = crs || crsOfImage(image);
    const [minX, minY, maxX, maxY] = image.getBoundingBox();
    const fullWidth = image.getWidth();
    const fullHeight = image.getHeight();
    const scale = Math.min(1, MAX_PIXELS / Math.max(fullWidth, fullHeight));
    const width = Math.max(1, Math.round(fullWidth * scale));
    const height = Math.max(1, Math.round(fullHeight * scale));

    const bands = image.getSamplesPerPixel();
    const format = (image.getSampleFormat && image.getSampleFormat()) || 1;
    const bits = image.getBitsPerSample();
    const rgb = bands >= 3 && bits === 8 && format === 1;
    const rasters = await tiff.readRasters({
      samples: rgb ? [0, 1, 2, ...(bands >= 4 ? [3] : [])] : [0],
      width,
      height,
      resampleMethod: "nearest",
    });
    const nodata = image.getGDALNoData();

    // Pixel (col, row) of a source-CRS point in the downsampled raster.
    const toPixel = (x, y) => [((x - minX) / (maxX - minX)) * width, ((maxY - y) / (maxY - minY)) * height];
    const geographic = source === WGS84 || source === "EPSG:4269" || source === "EPSG:4283";
    const fromWgs84 = geographic ? null : await transformer(WGS84, source, getDefinition);
    const toWgs84 = geographic ? null : await transformer(source, WGS84, getDefinition);
    const toSource = (lon, lat) => {
      if (fromWgs84) return fromWgs84(wrap(lon), lat);
      // A 0–360 geographic raster: put the longitude in its range.
      return [lon < minX - 1 ? lon + 360 : lon > maxX + 1 ? lon - 360 : lon, lat];
    };

    const bounds = lonLatBounds([minX, minY, maxX, maxY], toWgs84);
    const colour = rgb ? rgbColour(rasters, nodata) : singleBandColour(rasters[0], nodata);
    const [west, south, east, north] = bounds;
    // Split at 180° so each part is an ordinary lon/lat rectangle for the globe.
    const spans = east > 180 ? [[west, 180], [180, east]] : [[west, east]];
    const parts = spans.map(([w, e]) => {
      const share = (e - w) / (east - west);
      const partWidth = Math.max(1, Math.round(width * share));
      const rgba = warp(partWidth, height, [w, south, e, north], toSource, toPixel, width, height, colour.pixel);
      return {
        west: w >= 180 ? w - 360 : w,
        east: e > 180 ? e - 360 : e,
        south,
        north,
        width: partWidth,
        height,
        rgba,
      };
    });
    return {
      parts,
      crs: source,
      bands,
      rgb,
      rescale: colour.rescale,
      bounds: [west, south, east > 180 ? east - 360 : east, north],
    };
  }

  function wrap(lon) {
    return ((((lon + 180) % 360) + 360) % 360) - 180;
  }

  // Lon/lat box of a source-CRS box, by transforming points along its edges; east goes past
  // 180 (rather than wrapping) when the box crosses the antimeridian.
  function lonLatBounds([minX, minY, maxX, maxY], toWgs84) {
    const points = [];
    const n = 20;
    for (let i = 0; i <= n; i++) {
      const x = minX + ((maxX - minX) * i) / n;
      const y = minY + ((maxY - minY) * i) / n;
      points.push([x, minY], [x, maxY], [minX, y], [maxX, y]);
    }
    let lonLat = points.map(([x, y]) => (toWgs84 ? toWgs84(x, y) : [x > 180 ? x - 360 : x, y]));
    lonLat = lonLat.filter(([lon, lat]) => Number.isFinite(lon) && Number.isFinite(lat));
    let lons = lonLat.map(([lon]) => lon);
    if (Math.max(...lons) - Math.min(...lons) > 180) lons = lons.map((lon) => (lon < 0 ? lon + 360 : lon));
    const lats = lonLat.map(([, lat]) => lat);
    return [Math.min(...lons), Math.max(-90, Math.min(...lats)), Math.max(...lons), Math.min(90, Math.max(...lats))];
  }

  // Resample into a lon/lat grid of width x height covering bounds. The source position is
  // computed exactly on a coarse grid and interpolated between, which is quick and, for
  // the sizes a single image covers, indistinguishable from transforming every pixel.
  function warp(width, height, [west, south, east, north], toSource, toPixel, srcWidth, srcHeight, pixel) {
    const n = 32;
    const grid = new Float64Array((n + 1) * (n + 1) * 2);
    for (let gy = 0; gy <= n; gy++) {
      for (let gx = 0; gx <= n; gx++) {
        const lon = west + ((east - west) * gx) / n;
        const lat = north - ((north - south) * gy) / n;
        const [col, row] = toPixel(...toSource(lon, lat));
        const i = (gy * (n + 1) + gx) * 2;
        grid[i] = col;
        grid[i + 1] = row;
      }
    }
    const rgba = new Uint8ClampedArray(width * height * 4);
    for (let y = 0; y < height; y++) {
      const fy = ((y + 0.5) / height) * n;
      const gy = Math.min(n - 1, Math.floor(fy));
      const ty = fy - gy;
      for (let x = 0; x < width; x++) {
        const fx = ((x + 0.5) / width) * n;
        const gx = Math.min(n - 1, Math.floor(fx));
        const tx = fx - gx;
        const a = (gy * (n + 1) + gx) * 2;
        const b = a + 2;
        const c = a + (n + 1) * 2;
        const d = c + 2;
        const col = (1 - ty) * ((1 - tx) * grid[a] + tx * grid[b]) + ty * ((1 - tx) * grid[c] + tx * grid[d]);
        const row = (1 - ty) * ((1 - tx) * grid[a + 1] + tx * grid[b + 1]) + ty * ((1 - tx) * grid[c + 1] + tx * grid[d + 1]);
        if (!(col >= 0 && row >= 0 && col < srcWidth && row < srcHeight)) continue;
        pixel(Math.floor(row) * srcWidth + Math.floor(col), rgba, (y * width + x) * 4);
      }
    }
    return rgba;
  }

  function rgbColour(rasters, nodata) {
    const [r, g, b, alpha] = rasters;
    return {
      rescale: null,
      pixel(i, out, o) {
        if (alpha && alpha[i] === 0) return;
        if (nodata !== null && r[i] === nodata && g[i] === nodata && b[i] === nodata) return;
        out[o] = r[i];
        out[o + 1] = g[i];
        out[o + 2] = b[i];
        out[o + 3] = 255;
      },
    };
  }

  // Viridis over the 2nd–98th percentile, like the server's default.
  function singleBandColour(values, nodata) {
    const valid = (v) => Number.isFinite(v) && v !== nodata;
    const sample = [];
    const step = Math.max(1, Math.floor(values.length / 200000));
    for (let i = 0; i < values.length; i += step) if (valid(values[i])) sample.push(values[i]);
    sample.sort((p, q) => p - q);
    let low = sample.length ? sample[Math.floor(sample.length * 0.02)] : 0;
    let high = sample.length ? sample[Math.min(sample.length - 1, Math.floor(sample.length * 0.98))] : 1;
    if (high === low) high = low + 1;
    const lut = viridisTable();
    return {
      rescale: [low, high],
      pixel(i, out, o) {
        const v = values[i];
        if (!valid(v)) return;
        const t = Math.max(0, Math.min(255, Math.round(((v - low) / (high - low)) * 255)));
        out[o] = lut[t * 3];
        out[o + 1] = lut[t * 3 + 1];
        out[o + 2] = lut[t * 3 + 2];
        out[o + 3] = 255;
      },
    };
  }

  function viridisTable() {
    const stops = VIRIDIS.map((hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)));
    const lut = new Uint8Array(256 * 3);
    for (let t = 0; t < 256; t++) {
      const f = (t / 255) * (stops.length - 1);
      const i = Math.min(stops.length - 2, Math.floor(f));
      for (let k = 0; k < 3; k++) lut[t * 3 + k] = Math.round(stops[i][k] + (stops[i + 1][k] - stops[i][k]) * (f - i));
    }
    return lut;
  }

  root.LocalFiles = { browserKind, readGeoJSON, readShapefile, openPMTiles, readGeoTIFF };
})(typeof window !== "undefined" ? window : globalThis);
