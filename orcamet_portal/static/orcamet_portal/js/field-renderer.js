/*
 * OrcaMet Portal — client-side weather field renderer.
 *
 * The map used to show a PNG per variable per forecast hour, rendered by
 * matplotlib in the cron and stored in the database. That cost 5 variables x
 * 72 hours of images per run, made switching a variable a round trip per
 * frame, and placed an equirectangular image into a Web Mercator map — which
 * displaced every contour feature by up to 26 km until it was corrected.
 *
 * This paints the field in the browser instead, straight from the grid point
 * values the hover readout already fetches. Drawing happens in the map's own
 * projected space, so the projection is right by construction rather than by
 * a correction that has to be kept in step.
 *
 * Two properties of the server renderer are deliberately preserved, because
 * they are what make the picture honest rather than merely pretty:
 *
 *   1. A gap in the grid is not drawn. A rate-limited run can lose whole
 *      latitude bands, and interpolating across one renders invented weather
 *      in the most alarming colour on the scale. Cells further than
 *      MAX_GAP_CELLS from an observation are left transparent.
 *
 *   2. The surface never exceeds the values it was built from. Bilinear
 *      interpolation is bounded by its four corners by construction, which
 *      is a stronger guarantee than the server's cubic-then-clamp: there is
 *      no overshoot to clamp away.
 *
 * No external dependency. A contour library would give polygons; what this
 * needs is a coloured field, which is a raster, and the browser already has
 * one of those.
 */
(function (global) {
    'use strict';

    // How far from a real observation the field may still be painted, in
    // multiples of the grid's own spacing. Matches MAX_GAP_FACTOR in
    // forecasts/engine/map_interpolation.py.
    var MAX_GAP_CELLS = 1.5;

    // Colour bands. The server drew 51 filled contour levels; quantising to
    // the same count keeps the banded reading rather than a continuous wash,
    // so a boundary on the map still means a threshold crossed.
    var LEVELS = 51;

    function clamp(v, lo, hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }

    /*
     * Turn the flat point rows into a regular lat/lon lattice.
     *
     * The grid is built by np.arange in risk_grid, so the points are already
     * regular — but a run can be missing some of them, and the rows arrive in
     * whatever order the database returned. Indexing by rounded position
     * rather than by array order is what makes a partial run safe: a missing
     * point leaves a NaN hole instead of shifting every later point one place
     * along, which would smear the whole field sideways.
     */
    /*
     * The spacing of the grid the points were drawn from — the SMALLEST gap
     * between neighbouring coordinates, not the average one.
     *
     * This distinction is the whole reason a rate-limited run stays honest.
     * Deriving the step from the span divided by the number of distinct
     * values present gives 0.708 for a 0.5 degree grid missing five rows,
     * and every row above the gap is then placed too low — the gap is
     * silently squeezed out and the field looks complete. The minimum gap is
     * the true lattice pitch whether or not rows are missing.
     */
    function latticeStep(sorted) {
        var step = Infinity;
        for (var i = 1; i < sorted.length; i++) {
            var gap = sorted[i] - sorted[i - 1];
            if (gap > 1e-9 && gap < step) step = gap;
        }
        return isFinite(step) ? step : 0;
    }

    function buildLattice(points, valueIndex) {
        if (!points || !points.length) return null;

        var present = { lat: [], lon: [] };
        var seenLat = {}, seenLon = {};
        var i, p;

        for (i = 0; i < points.length; i++) {
            p = points[i];
            var la = Math.round(p[0] * 1000) / 1000;
            var lo = Math.round(p[1] * 1000) / 1000;
            if (!seenLat[la]) { seenLat[la] = true; present.lat.push(la); }
            if (!seenLon[lo]) { seenLon[lo] = true; present.lon.push(lo); }
        }
        if (present.lat.length < 2 || present.lon.length < 2) return null;

        present.lat.sort(function (a, b) { return a - b; });
        present.lon.sort(function (a, b) { return a - b; });

        var latStep = latticeStep(present.lat);
        var lonStep = latticeStep(present.lon);
        if (!latStep || !lonStep) return null;

        var lat0 = present.lat[0], lon0 = present.lon[0];

        // Size the lattice from the full span at the true pitch, so missing
        // rows and columns occupy their real places as holes.
        var ny = Math.round((present.lat[present.lat.length - 1] - lat0) / latStep) + 1;
        var nx = Math.round((present.lon[present.lon.length - 1] - lon0) / lonStep) + 1;
        if (ny < 2 || nx < 2) return null;

        var values = new Float64Array(nx * ny);
        for (i = 0; i < values.length; i++) values[i] = NaN;

        var min = Infinity, max = -Infinity, filled = 0;

        for (i = 0; i < points.length; i++) {
            p = points[i];
            var v = p[valueIndex];
            if (v == null || isNaN(v)) continue;

            var yi = Math.round((p[0] - lat0) / latStep);
            var xi = Math.round((p[1] - lon0) / lonStep);
            if (yi < 0 || yi >= ny || xi < 0 || xi >= nx) continue;

            values[yi * nx + xi] = v;
            if (v < min) min = v;
            if (v > max) max = v;
            filled++;
        }

        if (!filled) return null;

        return {
            lat0: lat0, lon0: lon0,
            nx: nx, ny: ny, values: values,
            min: min, max: max,
            latStep: latStep, lonStep: lonStep,
            // How much of the intended lattice actually carries data. A run
            // that lost a third of its points is worth being able to say so.
            coverage: filled / (nx * ny)
        };
    }

    /*
     * Bilinear sample with a hole rule.
     *
     * Returns NaN when any of the four surrounding cells is missing, rather
     * than falling back to the nearest one. Averaging around a hole is how a
     * gap gets painted as though it were measured — the very defect the
     * server's void blanking exists to prevent.
     */
    function sample(grid, lat, lon) {
        var fx = (lon - grid.lon0) / grid.lonStep;
        var fy = (lat - grid.lat0) / grid.latStep;

        if (fx < -MAX_GAP_CELLS || fy < -MAX_GAP_CELLS) return NaN;
        if (fx > grid.nx - 1 + MAX_GAP_CELLS) return NaN;
        if (fy > grid.ny - 1 + MAX_GAP_CELLS) return NaN;

        // Outside the lattice but within the tolerance: clamp onto the edge
        // so the field reaches the coast rather than stopping short of it.
        fx = clamp(fx, 0, grid.nx - 1);
        fy = clamp(fy, 0, grid.ny - 1);

        var x0 = Math.floor(fx), y0 = Math.floor(fy);
        var x1 = Math.min(x0 + 1, grid.nx - 1);
        var y1 = Math.min(y0 + 1, grid.ny - 1);
        var tx = fx - x0, ty = fy - y0;

        var v00 = grid.values[y0 * grid.nx + x0];
        var v10 = grid.values[y0 * grid.nx + x1];
        var v01 = grid.values[y1 * grid.nx + x0];
        var v11 = grid.values[y1 * grid.nx + x1];

        if (isNaN(v00) || isNaN(v10) || isNaN(v01) || isNaN(v11)) return NaN;

        return v00 * (1 - tx) * (1 - ty)
             + v10 * tx * (1 - ty)
             + v01 * (1 - tx) * ty
             + v11 * tx * ty;
    }

    /*
     * Colour for a value, as [r, g, b], from a sampled matplotlib ramp.
     * Quantised to LEVELS bands before lookup so the result reads as filled
     * contours rather than a continuous gradient.
     */
    function colourFor(ramp, vmin, vmax, value) {
        var t = (value - vmin) / (vmax - vmin);
        t = clamp(t, 0, 1);

        // Band first, then look up: the reverse would give a smooth wash.
        t = Math.round(t * (LEVELS - 1)) / (LEVELS - 1);

        var pos = t * (ramp.length - 1);
        var i0 = Math.floor(pos);
        var i1 = Math.min(i0 + 1, ramp.length - 1);
        var f = pos - i0;

        var a = ramp[i0], b = ramp[i1];
        return [
            a[0] + (b[0] - a[0]) * f,
            a[1] + (b[1] - a[1]) * f,
            a[2] + (b[2] - a[2]) * f
        ];
    }

    /*
     * Paint the field into an ImageData buffer.
     *
     * Takes latitude per row and longitude per column rather than a
     * per-pixel projection callback. In any cylindrical projection — Web
     * Mercator included — longitude depends only on x and latitude only on
     * y, so a full-screen paint needs width + height projections instead of
     * width * height of them. Measured on a 1280x504 canvas that is the
     * difference between roughly 470 ms and a few milliseconds, which is the
     * difference between a timeline that scrubs and one that stutters.
     *
     * The caller builds those two arrays from the map, so this function
     * stays ignorant of the projection: whatever Leaflet is doing, the field
     * lands where Leaflet says it should. That is also what makes the old
     * equirectangular-image-in-a-Mercator-map defect impossible here.
     */
    /*
     * The LEVELS band colours, resolved once. Interpolating the ramp per
     * pixel was most of the paint cost, and there are only ever LEVELS
     * distinct answers — so resolve them up front and index.
     */
    function buildBandTable(spec) {
        var table = new Uint8ClampedArray(LEVELS * 3);
        for (var i = 0; i < LEVELS; i++) {
            var value = spec.vmin + (spec.vmax - spec.vmin) * (i / (LEVELS - 1));
            var rgb = colourFor(spec.ramp, spec.vmin, spec.vmax, value);
            table[i * 3] = rgb[0];
            table[i * 3 + 1] = rgb[1];
            table[i * 3 + 2] = rgb[2];
        }
        return table;
    }

    function paint(ctx, width, height, grid, spec, latForRow, lonForCol, alpha) {
        // A canvas can be measured before its container has been laid out —
        // the map lives in a flex column, so the first paint can arrive at
        // zero by zero. createImageData throws on that, and the throw used
        // to escape into the boot promise chain and take the site markers
        // down with it. There is simply nothing to draw yet; the resize
        // handler will call back when there is.
        if (!width || !height || width < 1 || height < 1) return null;

        var image = ctx.createImageData(width, height);
        var data = image.data;
        var vmin = spec.vmin, vmax = spec.vmax;
        var opacity = Math.round(clamp(alpha == null ? 1 : alpha, 0, 1) * 255);

        var bands = spec._bands || (spec._bands = buildBandTable(spec));
        var scale = (LEVELS - 1) / (vmax - vmin);

        for (var y = 0; y < height; y++) {
            var lat = latForRow[y];
            var rowStart = y * width * 4;

            for (var x = 0; x < width; x++) {
                var value = sample(grid, lat, lonForCol[x]);
                if (isNaN(value)) continue;   // left fully transparent

                var band = Math.round((value - vmin) * scale);
                if (band < 0) band = 0;
                else if (band > LEVELS - 1) band = LEVELS - 1;

                var b = band * 3;
                var offset = rowStart + x * 4;
                data[offset] = bands[b];
                data[offset + 1] = bands[b + 1];
                data[offset + 2] = bands[b + 2];
                data[offset + 3] = opacity;
            }
        }

        ctx.putImageData(image, 0, 0);
        return image;
    }

    global.OrcaMetField = {
        MAX_GAP_CELLS: MAX_GAP_CELLS,
        LEVELS: LEVELS,
        buildLattice: buildLattice,
        sample: sample,
        colourFor: colourFor,
        paint: paint
    };
})(typeof window !== 'undefined' ? window : this);
