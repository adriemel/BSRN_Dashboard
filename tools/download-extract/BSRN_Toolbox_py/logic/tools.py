# coding=utf-8

"""
Tools (File utilities)
"""

import gzip
import os
import traceback

from logic.helper import Result


class Tools:

    @staticmethod
    def concatenate_files(lst_files, s_output_path, i_skip_lines=0, b_delete_originals=False):
        """
        Concatenate files into a single output file.
        For the first file, all lines are written.
        For subsequent files, the first i_skip_lines lines are skipped (header dedup).

        Args:
            lst_files (list): List of file paths
            s_output_path (str): Output file path
            i_skip_lines (int): Number of lines to skip from the beginning of each file (except the first)
            b_delete_originals (bool): Delete original files after concatenation

        Returns:
            Result
        """
        result = Result(name="Concatenate files")

        if not lst_files:
            result.add_err("No files to concatenate.")
            return result

        if not s_output_path:
            result.add_err("No output path specified.")
            return result

        i_total_lines = 0
        i_files_processed = 0

        try:
            with open(s_output_path, "w", encoding="utf-8") as f_out:
                for i, s_file in enumerate(lst_files):
                    try:
                        with open(s_file, "r", encoding="utf-8") as f_in:
                            lines = f_in.readlines()

                        if i == 0:
                            # First file: write all lines
                            f_out.writelines(lines)
                            i_total_lines += len(lines)
                        else:
                            # Subsequent files: skip header lines
                            lines_to_write = lines[i_skip_lines:]
                            f_out.writelines(lines_to_write)
                            i_total_lines += len(lines_to_write)

                        i_files_processed += 1
                    except Exception as ex:
                        result.add_warn(f"Could not read file: {s_file} ({ex})")

            result.add_info(f"Files processed: {i_files_processed}")
            result.add_info(f"Total lines written: {i_total_lines}")
            result.add_info(f"Output: {s_output_path}")

            if b_delete_originals:
                i_deleted = 0
                for s_file in lst_files:
                    try:
                        os.remove(s_file)
                        i_deleted += 1
                    except Exception as ex:
                        result.add_warn(f"Could not delete: {s_file} ({ex})")
                result.add_info(f"Original files deleted: {i_deleted}")

        except Exception as ex:
            result.add_err(f"Error writing output file: {s_output_path} ({ex})")

        return result

    @staticmethod
    def convert_eol_to_unix(lst_files, mode="windows"):
        """
        Convert line endings to Unix format (LF).

        Args:
            lst_files (list): List of file paths
            mode (str): "windows" to convert CRLF->LF, "mac9" to convert lone CR->LF

        Returns:
            Result
        """
        result = Result(name="Convert EOL to Unix")

        if not lst_files:
            result.add_err("No files to convert.")
            return result

        i_modified = 0

        for s_file in lst_files:
            try:
                with open(s_file, "rb") as f:
                    data = f.read()

                if mode == "windows":
                    new_data = data.replace(b"\r\n", b"\n")
                elif mode == "mac9":
                    # Replace lone \r with \n (but not \r\n)
                    # First protect \r\n, then replace remaining \r, then restore
                    new_data = data.replace(b"\r\n", b"\x00CRLF\x00")
                    new_data = new_data.replace(b"\r", b"\n")
                    new_data = new_data.replace(b"\x00CRLF\x00", b"\n")
                else:
                    result.add_warn(f"Unknown mode: {mode}")
                    continue

                if new_data != data:
                    with open(s_file, "wb") as f:
                        f.write(new_data)
                    i_modified += 1
                    result.add_info(f"Converted: {os.path.basename(s_file)}")
                else:
                    result.add_info(f"No change: {os.path.basename(s_file)}")

            except Exception as ex:
                result.add_warn(f"Error processing: {s_file} ({ex})")

        s_mode_label = "Windows (CRLF)" if mode == "windows" else "macOS 9 (CR)"
        result.add_info(f"Mode: {s_mode_label} -> Unix (LF)")
        result.add_info(f"Files modified: {i_modified} / {len(lst_files)}")

        return result

    @staticmethod
    def decompress_files(lst_files):
        """
        Decompress .gz files in-place (removes .gz extension).

        Args:
            lst_files (list): List of file paths

        Returns:
            Result: includes data key "new_filenames" mapping old->new paths
        """
        result = Result(name="Decompress files")

        if not lst_files:
            result.add_err("No files to decompress.")
            return result

        dic_new_filenames = {}
        i_decompressed = 0

        for s_file in lst_files:
            if not s_file.endswith(".gz"):
                result.add_warn(f"Skipped (not .gz): {os.path.basename(s_file)}")
                continue

            s_output = s_file[:-3]  # Remove .gz
            try:
                with gzip.open(s_file, "rb") as f_in:
                    data = f_in.read()
                with open(s_output, "wb") as f_out:
                    f_out.write(data)
                os.remove(s_file)
                dic_new_filenames[s_file] = s_output
                i_decompressed += 1
                result.add_info(f"Decompressed: {os.path.basename(s_file)} -> {os.path.basename(s_output)}")
            except Exception as ex:
                result.add_warn(f"Error decompressing: {s_file} ({ex})")

        result.add_info(f"Files decompressed: {i_decompressed} / {len(lst_files)}")
        result.add_data("new_filenames", dic_new_filenames)

        return result

    @staticmethod
    def compress_files(lst_files):
        """
        Compress files with gzip in-place (adds .gz extension).

        Args:
            lst_files (list): List of file paths

        Returns:
            Result: includes data key "new_filenames" mapping old->new paths
        """
        result = Result(name="Compress files")

        if not lst_files:
            result.add_err("No files to compress.")
            return result

        dic_new_filenames = {}
        i_compressed = 0

        for s_file in lst_files:
            if s_file.endswith(".gz"):
                result.add_warn(f"Skipped (already .gz): {os.path.basename(s_file)}")
                continue

            s_output = s_file + ".gz"
            try:
                with open(s_file, "rb") as f_in:
                    data = f_in.read()
                with gzip.open(s_output, "wb") as f_out:
                    f_out.write(data)
                os.remove(s_file)
                dic_new_filenames[s_file] = s_output
                i_compressed += 1
                result.add_info(f"Compressed: {os.path.basename(s_file)} -> {os.path.basename(s_output)}")
            except Exception as ex:
                result.add_warn(f"Error compressing: {s_file} ({ex})")

        result.add_info(f"Files compressed: {i_compressed} / {len(lst_files)}")
        result.add_data("new_filenames", dic_new_filenames)

        return result
