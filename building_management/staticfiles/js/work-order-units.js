(function () {
  const initUnitsWidget = function () {
    const unitField = document.querySelector("select[data-units-api-template]");
    if (!unitField) {
      return;
    }

    const unitsApiTemplate = unitField.dataset.unitsApiTemplate;
    if (!unitsApiTemplate) {
      return;
    }

    const buildingField = document.getElementById("id_building");
    const loadingText = unitField.dataset.loadingText || "Loading…";

    const setDisabledState = function (disabled, message) {
      unitField.innerHTML = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = message || "---------";
      unitField.appendChild(placeholder);
      unitField.value = "";
      unitField.disabled = disabled;
      unitField.setAttribute("aria-disabled", String(disabled));
      unitField.setAttribute("aria-busy", String(disabled));
    };

    const populateUnits = async function (buildingId, selectedId) {
      if (!buildingId) {
        setDisabledState(true, unitField.getAttribute("data-empty-label") || "---------");
        return;
      }

      setDisabledState(true, loadingText);

      const endpoint = unitsApiTemplate.replace("{id}", encodeURIComponent(buildingId));
      try {
        const response = await fetch(endpoint, { headers: { Accept: "application/json" } });
        if (!response.ok) {
          throw new Error(`Unable to fetch units for building ${buildingId}`);
        }
        const data = await response.json();
        unitField.innerHTML = "";

        const blankOption = document.createElement("option");
        blankOption.value = "";
        blankOption.textContent = "---------";
        unitField.appendChild(blankOption);

        data.forEach(function (unit) {
          const option = document.createElement("option");
          option.value = unit.id;
          option.textContent = unit.label || unit.number || `#${unit.id}`;
          if (String(unit.id) === String(selectedId)) {
            option.selected = true;
          }
          unitField.appendChild(option);
        });

        unitField.disabled = data.length === 0;
        unitField.setAttribute("aria-disabled", String(unitField.disabled));
      } catch (error) {
        console.warn(error);
        setDisabledState(true, "—");
      } finally {
        unitField.removeAttribute("aria-busy");
      }
    };

    const initialBuilding =
      unitField.dataset.initialBuilding || (buildingField ? buildingField.value : "");
    const selectedUnit = unitField.dataset.selectedUnit || unitField.value;

    if (initialBuilding) {
      populateUnits(initialBuilding, selectedUnit);
    } else {
      setDisabledState(true, unitField.getAttribute("data-empty-label") || "---------");
    }

    if (buildingField && buildingField.tagName !== "INPUT") {
      buildingField.addEventListener("change", function (event) {
        populateUnits(event.target.value, "");
      });
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initUnitsWidget);
  } else {
    initUnitsWidget();
  }
})();
