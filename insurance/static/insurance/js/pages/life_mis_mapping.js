
    // Life Plan Mapping: the product picker under the "Life grid products" box. It lists the grid products of the chosen
    // insurer (embedded by the page as JSON), filters them as you type, and adds the ticked ones, or one "start*" line,
    // to the box. The box stays the single source of truth: what is saved is exactly the lines in it.
    (function () {
        var data = document.getElementById('lifeProductsData');
        var insurer = document.getElementById('id_insurer');
        var search = document.getElementById('lifeProductSearch');
        var list = document.getElementById('lifeProductList');
        var box = document.getElementById('id_grid_products');
        if (!data || !insurer || !search || !list || !box) return;

        var products = {};
        try { products = JSON.parse(data.textContent) || {}; } catch (e) { products = {}; }
        var SHOWN = 300;
        var count = document.getElementById('lifeProductCount');

        function matches() {
            var all = products[insurer.value] || [];
            var text = search.value.trim().toLowerCase();
            return text ? all.filter(function (name) { return name.toLowerCase().indexOf(text) !== -1; }) : all;
        }

        function render() {
            var found = matches();
            list.innerHTML = '';
            found.slice(0, SHOWN).forEach(function (name) {
                var option = document.createElement('option');
                option.value = name;
                option.textContent = name;
                list.appendChild(option);
            });
            if (!insurer.value) count.textContent = 'Choose the insurer to list its grid products.';
            else if (found.length > SHOWN) count.textContent = found.length + ' products match; showing the first ' + SHOWN + ', type to narrow.';
            else count.textContent = found.length + ' product' + (found.length === 1 ? '' : 's') + ' of ' + insurer.value;
        }

        function lines() {
            return box.value.split('\n').map(function (line) { return line.trim(); }).filter(Boolean);
        }

        function add(entries) {
            var have = lines();
            entries.forEach(function (entry) { if (have.indexOf(entry) === -1) have.push(entry); });
            box.value = have.join('\n');
        }

        document.getElementById('lifeAddSelected').addEventListener('click', function () {
            add(Array.prototype.filter.call(list.options, function (o) { return o.selected; }).map(function (o) { return o.value; }));
        });
        document.getElementById('lifeAddPrefix').addEventListener('click', function () {
            var text = search.value.trim();
            if (text) add([text + '*']);
        });
        list.addEventListener('dblclick', function () {
            add(Array.prototype.filter.call(list.options, function (o) { return o.selected; }).map(function (o) { return o.value; }));
        });
        insurer.addEventListener('change', render);
        search.addEventListener('input', render);
        render();
    })();
